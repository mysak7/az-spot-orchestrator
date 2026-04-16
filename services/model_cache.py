"""Model caching service for Azure Blob Storage.

Manages finding and generating SAS URLs for cached models, and selecting
the best source region for server-side blob copies.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
UTC = timezone.utc  # py310 compat
from typing import Literal

from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import BlobSasPermissions, UserDelegationKey, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from config import get_settings
from db.cosmos import get_cache_container
from db.models import ModelCacheEntry

logger = logging.getLogger(__name__)

# User-delegation key cache — refreshed hourly (keys are valid up to 7 days).
_delegation_key: UserDelegationKey | None = None
_delegation_key_expiry: datetime | None = None


# Geographic coordinates (lat, lon) for Azure regions to determine proximity
_REGION_COORDS: dict[str, tuple[float, float]] = {
    "eastus": (37.8, -74.0),  # Virginia
    "westus2": (47.3, -119.9),  # Washington
    "eastus2": (36.7, -77.5),  # Virginia
    "westeurope": (52.3, 4.8),  # Amsterdam
    "northeurope": (53.3, -6.3),  # Dublin
    "swedencentral": (60.7, 17.1),  # Gävle
    "southeastasia": (1.4, 103.8),  # Singapore
    "australiaeast": (-33.8, 151.2),  # Sydney
    "japaneast": (35.6, 139.7),  # Tokyo
}


def _sanitize_identifier(model_identifier: str) -> str:
    """Sanitize model identifier for blob name (e.g. 'qwen2.5:1.5b' → 'qwen2.5-1.5b')."""
    return re.sub(r"[:/]", "-", model_identifier)


def _blob_name(model_identifier: str, region: str) -> str:
    """Generate blob name: {region}/{model_identifier_sanitized}.tar.lz4"""
    sanitized = _sanitize_identifier(model_identifier)
    return f"{region}/{sanitized}.tar.lz4"


async def _get_delegation_key() -> UserDelegationKey:
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


async def get_best_source(model_identifier: str, vm_region: str) -> dict:
    """Check if a cached blob exists for this model in the VM's region.

    Blobs are managed manually (or via Seed/Copy workflows).
    Returns source="blob" with a SAS download URL if found, or source="ollama" if not.
    """
    blob_name = _blob_name(model_identifier, vm_region)
    s = get_settings()
    account_url = f"https://{s.azure_storage_account_name}.blob.core.windows.net"

    async with DefaultAzureCredential() as credential:
        async with BlobServiceClient(account_url=account_url, credential=credential) as client:
            bc = client.get_blob_client(
                container=s.azure_storage_container_name,
                blob=blob_name,
            )
            try:
                await bc.get_blob_properties()
            except ResourceNotFoundError:
                logger.info("No cached blob for %s in %s — VM will use Ollama", model_identifier, vm_region)
                return {"source": "ollama"}

    logger.info("Found cached blob for %s in %s", model_identifier, vm_region)
    download_url = await _generate_sas_url(blob_name, "read")
    return {"source": "blob", "download_url": download_url}


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


async def update_download_progress(model_identifier: str, region: str, pct: int) -> None:
    """Update download progress percentage (0-100) for a cache entry in pulling phase."""
    container = get_cache_container()
    entry_id = f"{_sanitize_identifier(model_identifier)}-{region}"
    try:
        item = await container.read_item(entry_id, partition_key=entry_id)
        item["download_progress_pct"] = pct
        item["updated_at"] = datetime.now(UTC).isoformat()
        await container.upsert_item(item)
    except Exception as e:
        logger.warning("Failed to update download progress for %s in %s: %s", model_identifier, region, e)


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


async def get_blob_read_url(model_identifier: str, region: str) -> str:
    """Generate a read SAS URL for a cached blob in the given region."""
    blob = _blob_name(model_identifier, region)
    return await _generate_sas_url(blob, "read", expiry_hours=4)


async def find_best_copy_source(model_identifier: str, target_region: str) -> str | None:
    """Return the nearest available region to copy a model blob from, or None."""
    container = get_cache_container()
    query = "SELECT * FROM c WHERE c.model_identifier = @model_id AND c.status = @status"
    items: list[ModelCacheEntry] = []
    async for item in container.query_items(
        query=query,
        parameters=[
            {"name": "@model_id", "value": model_identifier},
            {"name": "@status", "value": "available"},
        ],
    ):
        items.append(ModelCacheEntry(**item))

    available_regions = [e.region for e in items if e.region != target_region]
    if not available_regions:
        return None
    return _nearest_region(target_region, available_regions)


async def mark_copy_started(model_identifier: str, target_region: str) -> None:
    """Create a placeholder cache entry with status='uploading' and phase='copying'."""
    container = get_cache_container()
    now = datetime.now(UTC).isoformat()
    entry = ModelCacheEntry(
        id=f"{_sanitize_identifier(model_identifier)}-{target_region}",
        model_identifier=model_identifier,
        region=target_region,
        blob_name=_blob_name(model_identifier, target_region),
        status="uploading",
        current_phase="copying",
        upload_started_at=now,
        created_at=now,
        updated_at=now,
    )
    await container.upsert_item(entry.model_dump())
    logger.info("Marked copy started for %s → %s", model_identifier, target_region)


async def delete_cache_entry(model_identifier: str, region: str) -> None:
    """Delete cache entry from Cosmos DB and delete the blob from Azure Storage."""
    from services.blob_client import blob_service_client

    s = get_settings()
    entry_id = f"{_sanitize_identifier(model_identifier)}-{region}"
    blob = _blob_name(model_identifier, region)

    container = get_cache_container()
    try:
        await container.delete_item(entry_id, partition_key=entry_id)
        logger.info("Deleted Cosmos cache entry %s", entry_id)
    except Exception as e:
        logger.warning("Could not delete Cosmos entry %s: %s", entry_id, e)

    try:
        async with blob_service_client() as client:
            bc = client.get_blob_client(
                container=s.azure_storage_container_name, blob=blob
            )
            await bc.delete_blob()
            logger.info("Deleted blob %s", blob)
    except Exception as e:
        logger.warning("Could not delete blob %s: %s", blob, e)


async def delete_all_cache_for_model(model_identifier: str) -> int:
    """Delete all cache entries (all regions) for a given model. Returns count deleted."""
    container = get_cache_container()
    query = "SELECT * FROM c WHERE c.model_identifier = @model_id"
    items: list[ModelCacheEntry] = []
    async for item in container.query_items(
        query=query,
        parameters=[{"name": "@model_id", "value": model_identifier}],
    ):
        items.append(ModelCacheEntry(**item))

    count = 0
    for entry in items:
        try:
            await delete_cache_entry(entry.model_identifier, entry.region)
            count += 1
        except Exception as e:
            logger.warning("Failed to delete %s/%s: %s", model_identifier, entry.region, e)
    return count


async def delete_all_cache_entries() -> int:
    """Delete every cache entry across all models and regions. Returns count deleted."""
    container = get_cache_container()
    items: list[ModelCacheEntry] = []
    async for item in container.query_items(query="SELECT * FROM c"):
        items.append(ModelCacheEntry(**item))

    count = 0
    for entry in items:
        try:
            await delete_cache_entry(entry.model_identifier, entry.region)
            count += 1
        except Exception as e:
            logger.warning("Failed to delete %s/%s: %s", entry.model_identifier, entry.region, e)
    return count


async def list_cache_entries() -> list[ModelCacheEntry]:
    """List all cached models with their metadata."""
    container = get_cache_container()
    entries = []
    async for item in container.query_items(query="SELECT * FROM c ORDER BY c.created_at DESC"):
        entries.append(ModelCacheEntry(**item))
    return entries
