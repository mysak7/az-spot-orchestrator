"""API routes for model cache management."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException

from api.deps import TemporalClient
from config import get_settings
from schemas.api import (
    CacheCompleteRequest,
    CacheFailedRequest,
    CacheProgressRequest,
    CacheSourceResponse,
    CopyBlobRequest,
    DownloadLogRequest,
    ModelCacheEntryResponse,
)
from services.model_cache import (
    delete_cache_entry,
    find_best_copy_source,
    get_best_source,
    list_cache_entries,
    mark_upload_failed,
    register_upload_complete,
    update_download_stats,
    update_upload_phase,
)
from temporal.types import CopyBlobInput, SeedBlobInput
from temporal.workflows.blob_copy import CopyBlobWorkflow
from temporal.workflows.seed_blob import SeedBlobWorkflow

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/storage/cache", response_model=list[ModelCacheEntryResponse])
async def list_cache() -> list[ModelCacheEntryResponse]:
    """List all cached models with metadata and statistics."""
    try:
        entries = await list_cache_entries()
        return [ModelCacheEntryResponse(**e.model_dump()) for e in entries]
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/storage/cache/source", response_model=CacheSourceResponse)
async def get_cache_source(
    model_identifier: str,
    region: str,
) -> CacheSourceResponse:
    """Get the best source for a model in a given region.

    Returns SAS URLs for either downloading from blob or uploading to blob,
    depending on whether the model is already cached.
    """
    try:
        source_info = await get_best_source(model_identifier, region)
        return CacheSourceResponse(**source_info)
    except Exception as e:
        logger.error("Failed to get cache source for %s in %s: %s", model_identifier, region, e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/storage/cache/complete")
async def register_cache_upload(req: CacheCompleteRequest) -> dict:
    """Register that a model has been successfully uploaded to blob storage."""
    try:
        await register_upload_complete(
            req.model_identifier,
            req.region,
            req.size_bytes,
            req.duration_seconds,
        )
        return {
            "status": "registered",
            "model_identifier": req.model_identifier,
            "region": req.region,
        }
    except Exception as e:
        logger.error("Failed to register cache upload: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/storage/cache/progress")
async def report_cache_progress(req: CacheProgressRequest) -> dict:
    """Update the current phase of an in-progress model upload.

    Called by the Spot VM at each milestone:
    - phase='pulling'   — ollama pull started
    - phase='archiving' — tar compression started
    - phase='uploading' — blob upload started
    """
    try:
        await update_upload_phase(req.model_identifier, req.region, req.phase)
        return {"status": "updated", "phase": req.phase}
    except Exception as e:
        logger.error("Failed to update cache progress: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/storage/cache/failed")
async def report_cache_failed(req: CacheFailedRequest) -> dict:
    """Mark a cache entry as failed when the VM encounters an error."""
    try:
        await mark_upload_failed(req.model_identifier, req.region)
        return {"status": "marked_failed"}
    except Exception as e:
        logger.error("Failed to mark cache as failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/storage/cache/download-log")
async def log_cache_download(req: DownloadLogRequest) -> dict:
    """Log download timing statistics for a cached model."""
    try:
        await update_download_stats(
            req.model_identifier,
            req.source_region,
            req.duration_seconds,
        )
        return {
            "status": "logged",
            "model_identifier": req.model_identifier,
            "source_region": req.source_region,
        }
    except Exception as e:
        logger.error("Failed to log cache download: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/storage/regions", response_model=list[str])
async def list_regions() -> list[str]:
    """Return the candidate Azure regions used for Spot VM provisioning."""
    return get_settings().azure_candidate_regions


@router.post("/storage/cache/copy", status_code=202)
async def copy_blob(req: CopyBlobRequest, temporal: TemporalClient) -> dict:
    """Start a CopyBlobWorkflow to replicate a cached model to a new region.

    Uses Azure server-side copy from the nearest available source — no data
    leaves Azure's backbone.  Returns 422 if no source blob exists yet; use
    POST /storage/cache/seed to provision a VM and create the initial blob.
    """
    source_region = await find_best_copy_source(req.model_identifier, req.target_region)
    if source_region is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No cached blob found for '{req.model_identifier}' in any region. "
                "Use the Seed action to provision a VM and create the initial blob."
            ),
        )

    s = get_settings()
    safe_id = req.model_identifier.replace(":", "-").replace(".", "-")
    workflow_id = f"copy-{safe_id}-to-{req.target_region}-{uuid.uuid4().hex[:8]}"
    try:
        await temporal.start_workflow(
            CopyBlobWorkflow.run,
            CopyBlobInput(
                model_identifier=req.model_identifier,
                target_region=req.target_region,
            ),
            id=workflow_id,
            task_queue=s.temporal_task_queue,
        )
    except Exception as e:
        logger.error("Failed to start CopyBlobWorkflow: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"status": "accepted", "workflow_id": workflow_id}


@router.post("/storage/cache/seed", status_code=202)
async def seed_blob_cache(req: CopyBlobRequest, temporal: TemporalClient) -> dict:
    """Start a SeedBlobWorkflow to pull a model from Ollama registry and upload it to blob storage.

    The control plane downloads layers directly from registry.ollama.ai — no VM
    is provisioned.  Once the blob is available, other regions can be populated
    via the faster server-side CopyBlobWorkflow.
    """
    s = get_settings()
    safe_id = req.model_identifier.replace(":", "-").replace(".", "-")
    workflow_id = f"seed-{safe_id}-{req.target_region}-{uuid.uuid4().hex[:8]}"
    try:
        await temporal.start_workflow(
            SeedBlobWorkflow.run,
            SeedBlobInput(
                model_identifier=req.model_identifier,
                target_region=req.target_region,
            ),
            id=workflow_id,
            task_queue=s.temporal_task_queue,
        )
    except Exception as e:
        logger.error("Failed to start SeedBlobWorkflow: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info(
        "Seeding blob cache for %s in %s (workflow %s)",
        req.model_identifier,
        req.target_region,
        workflow_id,
    )
    return {"status": "accepted", "workflow_id": workflow_id}


@router.delete("/storage/cache/entry")
async def delete_blob_entry(model_identifier: str, region: str) -> dict:
    """Delete a cached model blob from Azure Storage and remove its DB entry."""
    try:
        await delete_cache_entry(model_identifier, region)
        return {"status": "deleted", "model_identifier": model_identifier, "region": region}
    except Exception as e:
        logger.error("Failed to delete cache entry: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
