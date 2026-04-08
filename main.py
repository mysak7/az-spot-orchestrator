"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client

from api.routes import models, proxy
from config import get_settings
from db.cosmos import setup_cosmos


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    await setup_cosmos()
    app.state.temporal_client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
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


@app.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}
