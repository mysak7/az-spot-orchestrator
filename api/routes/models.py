"""Model registration, instance management, and VM lifecycle notification routes."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from fastapi import APIRouter, HTTPException, status

from api.deps import TemporalClient
from config import get_settings
from db.cosmos import get_instances_container, get_models_container
from db.models import LLMModel, VMInstance, VMStatus
from schemas.api import (
    LLMModelCreate,
    LLMModelResponse,
    ProvisionRequest,
    ProvisionResponse,
    VMEvictedNotification,
    VMInstanceResponse,
)
from temporal.types import ProvisionVMInput
from temporal.workflows.vm_provisioning import ProvisionVMWorkflow

router = APIRouter()


# ── Model registration ─────────────────────────────────────────────────────────

@router.post("/models", response_model=LLMModelResponse, status_code=status.HTTP_201_CREATED)
async def create_model(payload: LLMModelCreate) -> LLMModelResponse:
    container = get_models_container()

    # Uniqueness check on `name`
    existing = [
        item
        async for item in container.query_items(
            query="SELECT c.id FROM c WHERE c.name = @name",
            parameters=[{"name": "@name", "value": payload.name}],
            enable_cross_partition_query=True,
        )
    ]
    if existing:
        raise HTTPException(status_code=409, detail=f"Model '{payload.name}' already registered")

    model = LLMModel(**payload.model_dump())
    await container.create_item(body=model.model_dump())
    return LLMModelResponse(**model.model_dump())


@router.get("/models", response_model=list[LLMModelResponse])
async def list_models() -> list[LLMModelResponse]:
    container = get_models_container()
    items = [
        item
        async for item in container.query_items(
            query="SELECT * FROM c ORDER BY c.created_at DESC",
            enable_cross_partition_query=True,
        )
    ]
    return [LLMModelResponse(**item) for item in items]


@router.get("/models/{model_id}", response_model=LLMModelResponse)
async def get_model(model_id: str) -> LLMModelResponse:
    container = get_models_container()
    try:
        item = await container.read_item(item=model_id, partition_key=model_id)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Model not found")
    return LLMModelResponse(**item)


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(model_id: str) -> None:
    container = get_models_container()
    try:
        await container.delete_item(item=model_id, partition_key=model_id)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Model not found")


# ── VM provisioning ────────────────────────────────────────────────────────────

@router.post(
    "/models/{model_id}/provision",
    response_model=ProvisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def provision_model(
    model_id: str,
    payload: ProvisionRequest,
    temporal: TemporalClient,
) -> ProvisionResponse:
    """Start a ProvisionVMWorkflow for the given model.

    Creates a pending VMInstance document immediately so callers can poll
    /instances for status. The workflow then updates it as it progresses.
    """
    models_container = get_models_container()
    try:
        model_item = await models_container.read_item(item=model_id, partition_key=model_id)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Model not found")

    settings = get_settings()
    vm_size = payload.vm_size or model_item["vm_size"]
    vm_name = f"spot-{model_item['name'][:10]}-{uuid.uuid4().hex[:8]}"
    workflow_id = f"provision-{vm_name}"

    # Create pending instance record
    instance = VMInstance(
        id=vm_name,
        model_id=model_id,
        model_name=model_item["name"],
        vm_name=vm_name,
        resource_group=settings.azure_resource_group,
        status=VMStatus.pending,
        workflow_id=workflow_id,
    )
    instances_container = get_instances_container()
    await instances_container.create_item(body=instance.model_dump())

    await temporal.start_workflow(
        ProvisionVMWorkflow.run,
        ProvisionVMInput(
            model_id=model_id,
            model_name=model_item["name"],
            model_identifier=model_item["model_identifier"],
            vm_size=vm_size,
            vm_name=vm_name,
            resource_group=settings.azure_resource_group,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )

    return ProvisionResponse(vm_name=vm_name, workflow_id=workflow_id)


@router.get("/models/{model_id}/instances", response_model=list[VMInstanceResponse])
async def list_instances(model_id: str) -> list[VMInstanceResponse]:
    container = get_instances_container()
    items = [
        item
        async for item in container.query_items(
            query="SELECT * FROM c WHERE c.model_id = @mid ORDER BY c.created_at DESC",
            parameters=[{"name": "@mid", "value": model_id}],
            enable_cross_partition_query=True,
        )
    ]
    return [VMInstanceResponse(**item) for item in items]


# ── VM lifecycle notifications ────────────────────────────────────────────────

@router.post("/vms/{vm_name}/ready", status_code=status.HTTP_200_OK)
async def notify_vm_ready(vm_name: str) -> dict:
    """Cloud-init calls this after the model finishes downloading."""
    container = get_instances_container()
    try:
        item = await container.read_item(item=vm_name, partition_key=vm_name)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="VM not found")

    item["status"] = VMStatus.running.value
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    await container.replace_item(item=vm_name, body=item)
    return {"acknowledged": True}


@router.post("/vms/{vm_name}/evicted", status_code=status.HTTP_200_OK)
async def notify_vm_evicted(
    vm_name: str,
    payload: VMEvictedNotification,
    temporal: TemporalClient,
) -> dict:
    """Spot VM calls this on eviction (2-min Azure IMDS warning).

    Marks the instance as evicted and triggers a new ProvisionVMWorkflow
    so the model stays available with minimal downtime.
    """
    instances_container = get_instances_container()
    try:
        item = await instances_container.read_item(item=vm_name, partition_key=vm_name)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail="VM not found")

    item["status"] = VMStatus.evicted.value
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    await instances_container.replace_item(item=vm_name, body=item)

    # Trigger re-provisioning
    settings = get_settings()
    models_container = get_models_container()
    model_id = item["model_id"]
    try:
        model_item = await models_container.read_item(item=model_id, partition_key=model_id)
    except ResourceNotFoundError:
        return {"acknowledged": True, "re_provisioning": False}

    new_vm_name = f"spot-{model_item['name'][:10]}-{uuid.uuid4().hex[:8]}"
    workflow_id = f"provision-{new_vm_name}"

    new_instance = VMInstance(
        id=new_vm_name,
        model_id=model_id,
        model_name=model_item["name"],
        vm_name=new_vm_name,
        resource_group=settings.azure_resource_group,
        status=VMStatus.pending,
        workflow_id=workflow_id,
    )
    await instances_container.create_item(body=new_instance.model_dump())

    await temporal.start_workflow(
        ProvisionVMWorkflow.run,
        ProvisionVMInput(
            model_id=model_id,
            model_name=model_item["name"],
            model_identifier=model_item["model_identifier"],
            vm_size=model_item["vm_size"],
            vm_name=new_vm_name,
            resource_group=settings.azure_resource_group,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )

    return {"acknowledged": True, "re_provisioning": True, "new_vm_name": new_vm_name}
