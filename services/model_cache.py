"""Model caching service for Azure Blob Storage.

Manages finding and generating SAS URLs for cached models, tracking
upload/download statistics, and selecting the best source region.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from config import get_settings
from db.cosmos import get_cache_container
from db.models import ModelCacheEntry

logger = logging.getLogger(__name__)

# User-delegation key cache — refreshed hourly (keys are valid up to 7 days).
_delegation_key: object | None = None
_delegation_key_expiry: datetime | None = None


# Geographic coordinates (lat, lon) for Azure regions to determine proximity
_REGION_COORDS: dict[str, tuple[float, float]] = {
    "eastus": (37.8, -74.0),  # Virginia
    "westus2": (47.3, -119.9),  # Washington
    "eastus2": (36.7, -77.5),  # Virginia
    "westeurope": (52.3, 4.8),  # Amsterdam
    "northeurope": (60.5, 13.0),  # Dublin/Stockholm area
    "southeastasia": (1.4, 103.8),  # Singapore
    "australiaeast": (-33.8, 151.2),  # Sydney
    "japaneast": (35.6, 139.7),  # Tokyo
}


def _sanitize_identifier(model_identifier: str) -> str:
    """Sanitize model identifier for blob name (e.g. 'qwen2.5:1.5b' → 'qwen2.5-1.5b')."""
    return re.sub(r"[:/]", "-", model_identifier)


def _blob_name(model_identifier: str, region: str) -> str:
    """Generate blob name: {region}/{model_identifier_sanitized}.tar.gz"""
    sanitized = _sanitize_identifier(model_identifier)
    return f"{region}/{sanitized}.tar.gz"


async def _get_delegation_key() -> object:
    """Return a cached user-delegation key, refreshing when near expiry.

    Uses the service principal already configured for Azure VM operations —
    no storage account key needed anywhere.
    """
    global _delegation_key, _delegation_key_expiry
    now = datetime.now(UTC)
    if _delegation_key_expiry is not None and _delegation_key_expiry > now + timedelta(minutes=10):
        return _delegation_key  # type: ignore[return-value]

    s = get_settings()
    expiry = now + timedelta(hours=1)
    account_url = f"https://{s.azure_storage_account_name}.blob.core.windows.net"
    async with DefaultAzureCredential() as credential:
        async with BlobServiceClient(account_url=account_url, credential=credential) as client:
            key = await client.get_user_delegation_key(
                key_start_time=now - timedelta(minutes=5),  # small buffer for clock skew
                key_expiry_time=expiry,
            )

    _delegation_key = key
    _delegation_key_expiry = expiry
    return key


async def _generate_sas_url(
    blob_name: str,
    permission: Literal["read", "write"],
    expiry_hours: int = 2,
) -> str:
    """Generate a user-delegation SAS URL — no storage account key required."""
    s = get_settings()
    perms = BlobSasPermissions(
        read=(permission == "read"),
        write=(permission == "write"),
        create=(permission == "write"),
    )
    expiry = datetime.now(UTC) + timedelta(hours=expiry_hours)
    delegation_key = await _get_delegation_key()
    sas = generate_blob_sas(
        account_name=s.azure_storage_account_name,
        container_name=s.azure_storage_container_name,
        blob_name=blob_name,
        user_delegation_key=delegation_key,
        permission=perms,
        expiry=expiry,
    )
    account_url = f"https://{s.azure_storage_account_name}.blob.core.windows.net"
    return f"{account_url}/{s.azure_storage_container_name}/{blob_name}?{sas}"


def _euclidean_distance(region1: str, region2: str) -> float:
    """Calculate Euclidean distance between two regions based on coordinates."""
    if region1 not in _REGION_COORDS or region2 not in _REGION_COORDS:
        return float("inf")
    lat1, lon1 = _REGION_COORDS[region1]
    lat2, lon2 = _REGION_COORDS[region2]
    return math.sqrt((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2)


def _nearest_region(target_region: str, candidate_regions: list[str]) -> str | None:
    """Find the nearest region from candidates using geographic distance."""
    if not candidate_regions:
        return None
    if target_region in candidate_regions:
        return target_region
    nearest = min(
        candidate_regions,
        key=lambda r: _euclidean_distance(target_region, r),
    )
    return nearest if _euclidean_distance(target_region, nearest) < float("inf") else None


async def get_best_source(
    model_identifier: str,
    vm_region: str,
) -> dict:
    """Find the best source for a model and return download/upload URLs.

    Priority:
    1. Same region in Blob Storage → fastest
    2. Nearest region in Blob Storage → Azure-to-Azure cross-region
    3. Ollama (internet) → fallback, requires subsequent upload

    Returns a dict with keys:
    - source: "blob_same_region" | "blob_cross_region" | "ollama"
    - download_url: SAS URL to download from blob (if blob source)
    - upload_url: SAS URL to upload to blob (for Ollama fallback or initial VM)
    - source_region: the region from which blob is downloaded (if blob source)
    """
    container = get_cache_container()
    logger.info("Finding best source for model %s in region %s", model_identifier, vm_region)

    # Query all available (uploaded) cache entries for this model
    query = "SELECT * FROM c WHERE c.model_identifier = @model_id AND c.status = @status"
    items = []
    async for item in container.query_items(
        query=query,
        parameters=[
            {"name": "@model_id", "value": model_identifier},
            {"name": "@status", "value": "available"},
        ],
    ):
        items.append(ModelCacheEntry(**item))

    # Check if we have the model in the same region
    same_region_entry = next(
        (e for e in items if e.region == vm_region),
        None,
    )
    if same_region_entry:
        try:
            download_url = await _generate_sas_url(same_region_entry.blob_name, "read")
            upload_url = await _generate_sas_url(_blob_name(model_identifier, vm_region), "write")
            logger.info("Model %s found in same region %s", model_identifier, vm_region)
            return {
                "source": "blob_same_region",
                "download_url": download_url,
                "upload_url": upload_url,
                "source_region": vm_region,
            }
        except Exception as e:
            logger.warning(
                "Same-region blob found for %s but SAS generation failed — falling back: %s",
                model_identifier,
                e,
            )

    # Find nearest region with the model
    if items:
        available_regions = [e.region for e in items]
        nearest = _nearest_region(vm_region, available_regions)
        if nearest:
            nearest_entry = next(e for e in items if e.region == nearest)
            try:
                download_url = await _generate_sas_url(nearest_entry.blob_name, "read")
                upload_url = await _generate_sas_url(_blob_name(model_identifier, vm_region), "write")
                logger.info(
                    "Model %s not in %s, found in nearest region %s (distance=%.1f)",
                    model_identifier,
                    vm_region,
                    nearest,
                    _euclidean_distance(vm_region, nearest),
                )
                return {
                    "source": "blob_cross_region",
                    "download_url": download_url,
                    "upload_url": upload_url,
                    "source_region": nearest,
                }
            except Exception as e:
                logger.warning(
                    "Cross-region blob found for %s in %s but SAS generation failed — falling back: %s",
                    model_identifier,
                    nearest,
                    e,
                )

    # No cached model — fall back to Ollama pull.
    # Mark upload started *before* generating the SAS URL so that the
    # dashboard shows the "uploading" entry even if SAS generation fails.
    logger.info("No cached blob for model %s; falling back to Ollama pull", model_identifier)
    await mark_upload_started(model_identifier, vm_region)

    try:
        upload_url = await _generate_sas_url(_blob_name(model_identifier, vm_region), "write")
    except Exception as e:
        # Storage account not configured or credentials invalid.  The VM will
        # still pull from Ollama; it just won't be able to cache the result.
        logger.warning(
            "Cannot generate upload SAS URL for %s in %s — storage not configured? (%s)",
            model_identifier,
            vm_region,
            e,
        )
        upload_url = ""

    return {
        "source": "ollama",
        "upload_url": upload_url,
    }


async def register_upload_complete(
    model_identifier: str,
    region: str,
    size_bytes: int,
    duration_seconds: float,
) -> None:
    """Register that a model has been successfully uploaded to blob storage."""
    container = get_cache_container()
    now = datetime.now(UTC).isoformat()
    entry = ModelCacheEntry(
        id=f"{_sanitize_identifier(model_identifier)}-{region}",
        model_identifier=model_identifier,
        region=region,
        blob_name=_blob_name(model_identifier, region),
        size_bytes=size_bytes,
        status="available",
        current_phase=None,
        upload_started_at=now,
        upload_completed_at=now,
        upload_duration_seconds=duration_seconds,
        created_at=now,
        updated_at=now,
    )
    await container.upsert_item(entry.model_dump())
    logger.info(
        "Registered cache entry for %s in %s: %d bytes in %.1fs",
        model_identifier,
        region,
        size_bytes,
        duration_seconds,
    )


async def update_download_stats(
    model_identifier: str,
    source_region: str,
    download_duration_seconds: float,
) -> None:
    """Update download statistics for a cache entry."""
    container = get_cache_container()
    entry_id = f"{_sanitize_identifier(model_identifier)}-{source_region}"

    try:
        item = await container.read_item(entry_id, partition_key=entry_id)
        entry = ModelCacheEntry(**item)
        entry.last_download_duration_seconds = download_duration_seconds
        entry.download_count += 1
        entry.updated_at = datetime.now(UTC).isoformat()
        await container.upsert_item(entry.model_dump())
        logger.info(
            "Updated download stats for %s from %s: %.1fs (count=%d)",
            model_identifier,
            source_region,
            download_duration_seconds,
            entry.download_count,
        )
    except Exception as e:
        logger.warning(
            "Failed to update download stats for %s from %s: %s",
            model_identifier,
            source_region,
            e,
        )


async def mark_upload_started(model_identifier: str, region: str) -> None:
    """Create a placeholder cache entry with status='uploading' and phase='pulling'."""
    container = get_cache_container()
    now = datetime.now(UTC).isoformat()
    entry = ModelCacheEntry(
        id=f"{_sanitize_identifier(model_identifier)}-{region}",
        model_identifier=model_identifier,
        region=region,
        blob_name=_blob_name(model_identifier, region),
        status="uploading",
        current_phase="pulling",
        upload_started_at=now,
        created_at=now,
        updated_at=now,
    )
    await container.upsert_item(entry.model_dump())
    logger.info("Marked upload started for %s in %s (phase=pulling)", model_identifier, region)


async def update_upload_phase(model_identifier: str, region: str, phase: str) -> None:
    """Update the current phase of an in-progress upload (pulling/archiving/uploading)."""
    container = get_cache_container()
    entry_id = f"{_sanitize_identifier(model_identifier)}-{region}"
    try:
        item = await container.read_item(entry_id, partition_key=entry_id)
        item["current_phase"] = phase
        item["updated_at"] = datetime.now(UTC).isoformat()
        await container.upsert_item(item)
        logger.info("Phase update for %s in %s: %s", model_identifier, region, phase)
    except Exception as e:
        logger.warning("Failed to update phase for %s in %s: %s", model_identifier, region, e)


async def mark_upload_failed(model_identifier: str, region: str) -> None:
    """Mark a cache entry as failed."""
    container = get_cache_container()
    entry_id = f"{_sanitize_identifier(model_identifier)}-{region}"
    try:
        item = await container.read_item(entry_id, partition_key=entry_id)
        item["status"] = "failed"
        item["current_phase"] = None
        item["updated_at"] = datetime.now(UTC).isoformat()
        await container.upsert_item(item)
        logger.warning("Marked upload failed for %s in %s", model_identifier, region)
    except Exception as e:
        logger.warning("Failed to mark upload failed for %s in %s: %s", model_identifier, region, e)


async def list_cache_entries() -> list[ModelCacheEntry]:
    """List all cached models with their metadata."""
    container = get_cache_container()
    entries = []
    async for item in container.query_items(query="SELECT * FROM c ORDER BY c.created_at DESC"):
        entries.append(ModelCacheEntry(**item))
    return entries
