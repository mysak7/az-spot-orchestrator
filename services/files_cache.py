"""Azure Files NFS share management — create accounts, shares, and track model status."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
UTC = timezone.utc  # py310 compat

from azure.core.exceptions import ResourceNotFoundError

from config import get_settings
from db.cosmos import get_files_container
from db.models import FilesShareEntry

logger = logging.getLogger(__name__)


def sanitize_identifier(model_identifier: str) -> str:
    return re.sub(r"[:/]", "-", model_identifier)


def files_account_name(region: str) -> str:
    """Derive the FileStorage account name for a region.

    Azure storage accounts: 3-24 chars, lowercase alphanumeric only.
    e.g. "azspotfiles" + "westeurope" = "azspotfileswesteurope" (21 chars).
    """
    s = get_settings()
    name = f"{s.azure_files_account_prefix}{region}".lower()
    # Strip any non-alphanumeric chars (e.g. if region somehow has dashes)
    name = re.sub(r"[^a-z0-9]", "", name)
    return name[:24]


async def get_files_entry(model_identifier: str, region: str) -> FilesShareEntry | None:
    """Return FilesShareEntry if one exists (any status), else None."""
    container = get_files_container()
    entry_id = f"{sanitize_identifier(model_identifier)}-{region}"
    try:
        item = await container.read_item(entry_id, partition_key=entry_id)
        return FilesShareEntry(**item)
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Failed to read files entry %s: %s", entry_id, exc)
        return None


async def get_available_files_entry(model_identifier: str, region: str) -> FilesShareEntry | None:
    """Return FilesShareEntry only if status == 'available', else None."""
    entry = await get_files_entry(model_identifier, region)
    if entry and entry.status == "available":
        return entry
    return None


async def mark_files_provisioning(model_identifier: str, region: str, account_key: str = "") -> None:
    container = get_files_container()
    now = datetime.now(UTC).isoformat()
    account = files_account_name(region)
    s = get_settings()
    entry = FilesShareEntry(
        id=f"{sanitize_identifier(model_identifier)}-{region}",
        model_identifier=model_identifier,
        region=region,
        storage_account=account,
        share_name=s.azure_files_share_name,
        account_key=account_key,
        status="provisioning",
        created_at=now,
        updated_at=now,
    )
    await container.upsert_item(entry.model_dump())
    logger.info("Marked files provisioning for %s in %s", model_identifier, region)


async def mark_files_available(model_identifier: str, region: str, size_bytes: int, account_key: str = "") -> None:
    container = get_files_container()
    entry_id = f"{sanitize_identifier(model_identifier)}-{region}"
    try:
        item = await container.read_item(entry_id, partition_key=entry_id)
    except ResourceNotFoundError:
        item = {}

    now = datetime.now(UTC).isoformat()
    account = files_account_name(region)
    s = get_settings()
    item.update({
        "id": entry_id,
        "model_identifier": model_identifier,
        "region": region,
        "storage_account": account,
        "share_name": s.azure_files_share_name,
        "size_bytes": size_bytes,
        "status": "available",
        "updated_at": now,
    })
    if account_key:
        item["account_key"] = account_key
    if "created_at" not in item:
        item["created_at"] = now
    await container.upsert_item(item)
    logger.info("Marked files available for %s in %s (%d bytes)", model_identifier, region, size_bytes)


async def mark_files_failed(model_identifier: str, region: str) -> None:
    container = get_files_container()
    entry_id = f"{sanitize_identifier(model_identifier)}-{region}"
    try:
        item = await container.read_item(entry_id, partition_key=entry_id)
        item["status"] = "failed"
        item["updated_at"] = datetime.now(UTC).isoformat()
        await container.upsert_item(item)
    except Exception as exc:
        logger.warning("Failed to mark files failed for %s in %s: %s", model_identifier, region, exc)


async def list_files_entries() -> list[FilesShareEntry]:
    container = get_files_container()
    entries = []
    async for item in container.query_items(query="SELECT * FROM c ORDER BY c.created_at DESC"):
        entries.append(FilesShareEntry(**item))
    return entries
