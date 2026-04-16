"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from temporalio.client import Client

from api.routes import images, inventory, messages, models, proxy, storage
from config import get_settings
from db.cosmos import seed_default_models, setup_cosmos

logger = logging.getLogger(__name__)


async def _warm_inventory_cache() -> None:
    """Pre-populate the Spot price inventory cache in the background at startup."""
    try:
        logger.info("Warming Spot inventory cache…")
        await inventory.get_spot_inventory(family=None, gpu_only=False, region=None, refresh=True)
        logger.info("Spot inventory cache ready")
    except Exception as exc:
        logger.warning("Spot inventory cache warm-up failed (will retry on first request): %s", exc)


async def _check_keep_alive_models(app: FastAPI) -> None:
    """Provision a new VM for any keep_alive model that has no healthy active VM.

    A VM is considered healthy-active if it is in pending/provisioning/downloading/running
    AND has not been stuck in a pre-running state for more than STALE_MINUTES.
    Stale instances are marked 'terminated' so the watchdog (and the UI) can move on.
    """
    import uuid
    from datetime import datetime, timezone
    UTC = timezone.utc  # py310 compat, timedelta

    from db.cosmos import get_instances_container, get_models_container
    from db.models import VMInstance, VMStatus
    from temporal.types import ProvisionVMInput
    from temporal.workflows.vm_provisioning import ProvisionVMWorkflow

    # If a VM has been stuck in pending/provisioning/downloading for this long, treat
    # it as dead (workflow never started or crashed without cleaning up).
    STALE_MINUTES = 20

    settings = get_settings()
    models_container = get_models_container()
    instances_container = get_instances_container()

    keep_alive_models = [
        item
        async for item in models_container.query_items(
            query="SELECT * FROM c WHERE c.keep_alive = true",
        )
    ]
    if not keep_alive_models:
        return

    now = datetime.now(UTC)

    # Collect model_ids that already have a non-terminal, non-stale VM
    active_raw = [
        item
        async for item in instances_container.query_items(
            query=(
                "SELECT * FROM c"
                " WHERE c.status != 'evicted' AND c.status != 'terminated'"
            ),
        )
    ]

    active_model_ids: set[str] = set()
    for item in active_raw:
        mid = item.get("model_id")
        if not mid:
            continue
        status = item.get("status", "")
        if status == "running":
            # Running VMs are always considered healthy.
            active_model_ids.add(mid)
            continue
        # For pre-running states, check if the instance is stale.
        created_raw = item.get("created_at") or item.get("updated_at")
        if created_raw:
            try:
                created = datetime.fromisoformat(created_raw)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                age = now - created
                if age < timedelta(minutes=STALE_MINUTES):
                    active_model_ids.add(mid)
                    continue
            except ValueError:
                active_model_ids.add(mid)
                continue
        # Stale non-running instance — mark terminated so the UI reflects reality.
        item["status"] = VMStatus.terminated.value
        item["updated_at"] = now.isoformat()
        try:
            await instances_container.replace_item(item=item["id"], body=item)
            logger.warning(
                "Keep-alive watchdog: marked stale instance '%s' (model=%s, was %s) as terminated",
                item["id"], mid, status,
            )
        except Exception as exc:
            logger.warning("Failed to mark stale instance terminated: %s", exc)
            active_model_ids.add(mid)  # skip to avoid double-provisioning on error

    for model_item in keep_alive_models:
        model_id = model_item["id"]
        if model_id in active_model_ids:
            continue

        new_vm_name = f"spot-{model_item['name'][:10].rstrip('-')}-{uuid.uuid4().hex[:8]}"
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

        await app.state.temporal_client.start_workflow(
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
        logger.info(
            "Keep-alive watchdog: started ProvisionVMWorkflow %s for model=%s",
            workflow_id,
            model_item["name"],
        )


async def _keep_alive_watchdog(app: FastAPI) -> None:
    """Every 60 s, ensure keep_alive models have an active VM."""
    await asyncio.sleep(15)  # short startup delay
    while True:
        try:
            await _check_keep_alive_models(app)
        except Exception as exc:
            logger.warning("Keep-alive watchdog error: %s", exc)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    await setup_cosmos()
    await seed_default_models()
    app.state.temporal_client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
    # Warm the inventory cache in the background — don't block startup
    asyncio.create_task(_warm_inventory_cache())
    asyncio.create_task(_keep_alive_watchdog(app))
    yield


app = FastAPI(
    title="Azure Spot LLM Orchestrator",
    description=(
        "Control plane for dynamically provisioning Azure Spot VMs running LLMs. "
        "Routes inference traffic to the cheapest available VM and re-provisions on eviction."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(models.router, prefix="/api", tags=["models"])
app.include_router(proxy.router, tags=["proxy"])
app.include_router(storage.router, prefix="/api", tags=["storage"])
app.include_router(inventory.router, prefix="/api", tags=["inventory"])
app.include_router(messages.router, prefix="/api", tags=["messages"])
app.include_router(images.router, prefix="/api", tags=["images"])

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/static/dashboard.html")


@app.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}
