"""API route for the Images state overview.

Joins models, blob cache, NFS shares, and VM instances into a single
model-centric response — one row per registered LLM model.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, HTTPException

from db.cosmos import (
    get_cache_container,
    get_files_container,
    get_instances_container,
    get_models_container,
)

log = structlog.get_logger()

router = APIRouter()


async def _collect(aiter) -> list[dict]:
    return [item async for item in aiter]


@router.get("/images/state")
async def get_images_state() -> list[dict]:
    """Return a joined view of all models with their blob cache, NFS share, and VM state.

    Each element in the returned list represents one registered LLM model and includes:
    - ``blobs``      — list of per-region blob cache entries (status, size, phase)
    - ``nfs_shares`` — list of per-region Azure Files NFS shares (status, size)
    - ``active_vms`` — list of non-terminated VM instances for this model
    - ``running_vms``— count of VMs currently in ``running`` state
    """
    try:
        models_container = get_models_container()
        cache_container = get_cache_container()
        files_container = get_files_container()
        instances_container = get_instances_container()

        models_raw, cache_raw, files_raw, instances_raw = await asyncio.gather(
            _collect(models_container.query_items(query="SELECT * FROM c")),
            _collect(cache_container.query_items(query="SELECT * FROM c")),
            _collect(files_container.query_items(query="SELECT * FROM c")),
            _collect(instances_container.query_items(
                query="SELECT * FROM c WHERE c.status != 'terminated'",
            )),
        )

        # Index blob cache entries by model_identifier
        blobs_by_identifier: dict[str, list[dict]] = {}
        for entry in cache_raw:
            mid = entry.get("model_identifier", "")
            blobs_by_identifier.setdefault(mid, []).append({
                "region": entry.get("region"),
                "status": entry.get("status"),
                "size_bytes": entry.get("size_bytes", 0),
                "current_phase": entry.get("current_phase"),
                "download_progress_pct": entry.get("download_progress_pct"),
                "updated_at": entry.get("updated_at"),
            })

        # Index NFS share entries by model_identifier
        nfs_by_identifier: dict[str, list[dict]] = {}
        for entry in files_raw:
            mid = entry.get("model_identifier", "")
            nfs_by_identifier.setdefault(mid, []).append({
                "region": entry.get("region"),
                "status": entry.get("status"),
                "size_bytes": entry.get("size_bytes", 0),
            })

        # Index VM instances by model_id (Cosmos document id, not model_identifier)
        vms_by_model_id: dict[str, list[dict]] = {}
        for inst in instances_raw:
            mid = inst.get("model_id")
            if not mid:
                continue
            vms_by_model_id.setdefault(mid, []).append({
                "vm_name": inst.get("vm_name"),
                "status": inst.get("status"),
                "region": inst.get("region"),
                "ip_address": inst.get("ip_address"),
            })

        result: list[dict] = []
        for model in models_raw:
            model_id = model["id"]
            model_identifier = model.get("model_identifier", "")
            vms = vms_by_model_id.get(model_id, [])
            result.append({
                "model_id": model_id,
                "model_name": model.get("name"),
                "model_identifier": model_identifier,
                "size_mb": model.get("size_mb", 0),
                "vm_size": model.get("vm_size"),
                "keep_alive": model.get("keep_alive", False),
                "blobs": blobs_by_identifier.get(model_identifier, []),
                "nfs_shares": nfs_by_identifier.get(model_identifier, []),
                "running_vms": sum(1 for v in vms if v["status"] == "running"),
                "active_vms": vms,
            })

        return result

    except Exception as exc:
        log.error("images_state_fetch_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
