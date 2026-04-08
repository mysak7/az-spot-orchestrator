"""Model registration, instance management, and VM lifecycle notification routes."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from temporalio.client import Client

from api.deps import DBSession, TemporalClient
from config import get_settings
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
async def create_model(payload: LLMModelCreate, db: DBSession) -> LLMModel:
    """Register a new LLM model in the image selector."""
    existing = await db.scalar(select(LLMModel).where(LLMModel.name == payload.name))
    if existing:
        raise HTTPException(status_code=409, detail=f"Model '{payload.name}' already registered")

    model = LLMModel(**payload.model_dump())
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


@router.get("/models", response_model=list[LLMModelResponse])
async def list_models(db: DBSession) -> list[LLMModel]:
    result = await db.execute(select(LLMModel).order_by(LLMModel.created_at.desc()))
    return list(result.scalars().all())


@router.get("/models/{model_id}", response_model=LLMModelResponse)
async def get_model(model_id: uuid.UUID, db: DBSession) -> LLMModel:
    model = await db.get(LLMModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(model_id: uuid.UUID, db: DBSession) -> None:
    model = await db.get(LLMModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    await db.delete(model)
    await db.commit()


# ── VM provisioning ────────────────────────────────────────────────────────────

@router.post(
    "/models/{model_id}/provision",
    response_model=ProvisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def provision_model(
    model_id: uuid.UUID,
    payload: ProvisionRequest,
    db: DBSession,
    temporal: TemporalClient,
) -> ProvisionResponse:
    """Trigger a ProvisionVMWorkflow for the given model.

    Creates a pending VMInstance record immediately and starts the Temporal
    workflow asynchronously. The workflow updates the record as it progresses.
    """
    model = await db.get(LLMModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    settings = get_settings()
    vm_size = payload.vm_size or model.vm_size
    vm_name = f"spot-{model.name[:10]}-{uuid.uuid4().hex[:8]}"
    workflow_id = f"provision-{vm_name}"
    resource_group = settings.azure_resource_group

    # Create a pending record so callers can poll /instances immediately
    instance = VMInstance(
        id=uuid.uuid4(),
        model_id=model_id,
        vm_name=vm_name,
        resource_group=resource_group,
        status=VMStatus.pending,
        workflow_id=workflow_id,
    )
    db.add(instance)
    await db.commit()

    await temporal.start_workflow(
        ProvisionVMWorkflow.run,
        ProvisionVMInput(
            model_id=str(model_id),
            model_name=model.name,
            model_identifier=model.model_identifier,
            vm_size=vm_size,
            vm_name=vm_name,
            resource_group=resource_group,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )

    return ProvisionResponse(vm_name=vm_name, workflow_id=workflow_id)


@router.get("/models/{model_id}/instances", response_model=list[VMInstanceResponse])
async def list_instances(model_id: uuid.UUID, db: DBSession) -> list[VMInstance]:
    """Return all VM instances for a model, newest first."""
    result = await db.execute(
        select(VMInstance)
        .where(VMInstance.model_id == model_id)
        .order_by(VMInstance.created_at.desc())
    )
    return list(result.scalars().all())


# ── VM lifecycle notifications (called by the Spot VM itself) ─────────────────

@router.post("/vms/{vm_name}/ready", status_code=status.HTTP_200_OK)
async def notify_vm_ready(vm_name: str, db: DBSession) -> dict:
    """Cloud-init calls this endpoint after the model finishes downloading."""
    instance = await db.scalar(select(VMInstance).where(VMInstance.vm_name == vm_name))
    if not instance:
        raise HTTPException(status_code=404, detail="VM not found")

    instance.status = VMStatus.running
    await db.commit()
    return {"acknowledged": True}


@router.post("/vms/{vm_name}/evicted", status_code=status.HTTP_200_OK)
async def notify_vm_evicted(
    vm_name: str,
    payload: VMEvictedNotification,
    db: DBSession,
    temporal: TemporalClient,
) -> dict:
    """Spot VM calls this endpoint on eviction (2-minute warning from Azure IMDS).

    Marks the current instance as evicted and immediately triggers a new
    ProvisionVMWorkflow so the model stays available with minimal downtime.
    """
    instance = await db.scalar(select(VMInstance).where(VMInstance.vm_name == vm_name))
    if not instance:
        raise HTTPException(status_code=404, detail="VM not found")

    instance.status = VMStatus.evicted
    await db.commit()

    # Trigger re-provisioning for the same model
    model = await db.get(LLMModel, instance.model_id)
    if model:
        settings = get_settings()
        new_vm_name = f"spot-{model.name[:10]}-{uuid.uuid4().hex[:8]}"
        workflow_id = f"provision-{new_vm_name}"

        new_instance = VMInstance(
            id=uuid.uuid4(),
            model_id=model.id,
            vm_name=new_vm_name,
            resource_group=settings.azure_resource_group,
            status=VMStatus.pending,
            workflow_id=workflow_id,
        )
        db.add(new_instance)
        await db.commit()

        await temporal.start_workflow(
            ProvisionVMWorkflow.run,
            ProvisionVMInput(
                model_id=str(model.id),
                model_name=model.name,
                model_identifier=model.model_identifier,
                vm_size=model.vm_size,
                vm_name=new_vm_name,
                resource_group=settings.azure_resource_group,
            ),
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )

    return {"acknowledged": True, "re_provisioning": model is not None}
