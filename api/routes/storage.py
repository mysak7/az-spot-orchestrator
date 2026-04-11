"""API routes for model cache management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from schemas.api import (
    CacheCompleteRequest,
    CacheSourceResponse,
    DownloadLogRequest,
    ModelCacheEntryResponse,
)
from services.model_cache import (
    get_best_source,
    list_cache_entries,
    register_upload_complete,
    update_download_stats,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/storage/cache", response_model=list[ModelCacheEntryResponse])
async def list_cache() -> list[ModelCacheEntryResponse]:
    """List all cached models with metadata and statistics."""
    entries = await list_cache_entries()
    return [ModelCacheEntryResponse(**e.model_dump()) for e in entries]


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
