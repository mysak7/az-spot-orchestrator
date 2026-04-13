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

from api.routes import inventory, models, proxy, storage
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

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/static/dashboard.html")


@app.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}
