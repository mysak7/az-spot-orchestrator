"""Spot VM price inventory endpoint.

Queries the Azure Retail Prices API for all virtualMachines Spot prices,
groups them by VM size / region, and returns a sorted inventory.
Results are cached in-process for 1 hour to avoid hammering the pricing API.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi import Request as FastAPIRequest
from temporalio.client import WorkflowFailureError

router = APIRouter()
logger = logging.getLogger(__name__)

_PRICING_URL = "https://prices.azure.com/api/retail/prices"
_CACHE_TTL = 3600  # seconds

# Simple in-process cache: (timestamp, payload)
_cache: tuple[float, dict] | None = None

# GPU label keyed by substring of armSkuName (lower)
_GPU_MAP: dict[str, str] = {
    "nc4as_t4":  "T4",
    "nc8as_t4":  "T4",
    "nc16as_t4": "T4",
    "nc64as_t4": "T4",
    "nd96asr":   "A100 80 GB",
    "nd96amsr":  "A100 80 GB",
    "nd40rs":    "V100",
    "nd6s":      "P40",
    "nd12s":     "P40",
    "nd24rs":    "P40",
    "nv6":       "M60",
    "nv12":      "M60",
    "nv24":      "M60",
    "nga10v2":   "A10",
    "nva10v2":   "A10",
}
_GPU_FAMILY_PREFIXES = ("nc", "nd", "nv", "ng")


def _gpu_label(sku: str) -> str | None:
    low = sku.lower()
    for k, v in _GPU_MAP.items():
        if k in low:
            return v
    # Fallback: known GPU families
    # Strip "standard_" prefix then check first two chars
    bare = low.removeprefix("standard_")
    if any(bare.startswith(p) for p in _GPU_FAMILY_PREFIXES):
        return "GPU"
    return None


def _family(sku: str) -> str:
    m = re.match(r"Standard_([A-Za-z]+)", sku)
    return m.group(1).upper() if m else "Other"


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    max_retries: int = 5,
) -> httpx.Response:
    """GET with exponential back-off on 429 / 5xx responses."""
    delay = 2.0
    for attempt in range(max_retries):
        resp = await client.get(url, params=params)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", delay))
            wait = max(retry_after, delay)
            logger.warning("429 from pricing API (attempt %d/%d) — waiting %.0f s", attempt + 1, max_retries, wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code >= 500:
            logger.warning("HTTP %s from pricing API (attempt %d/%d)", resp.status_code, attempt + 1, max_retries)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        resp.raise_for_status()
        return resp
    raise httpx.HTTPStatusError(
        f"Pricing API still returning errors after {max_retries} retries",
        request=resp.request,  # type: ignore[possibly-undefined]
        response=resp,  # type: ignore[possibly-undefined]
    )


async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch one page; return (items, next_url)."""
    resp = await _get_with_retry(client, url, params=params)
    data = resp.json()
    items = [
        item for item in data.get("Items", [])
        if item.get("armSkuName") and item.get("armRegionName")
    ]
    return items, data.get("NextPageLink") or None


async def _fetch_region(client: httpx.AsyncClient, region: str) -> list[dict]:
    """Fetch all Spot VM price rows for one Azure region."""
    rows: list[dict] = []
    url: str | None = _PRICING_URL
    params: dict | None = {
        "api-version": "2023-01-01-preview",
        "$filter": (
            "priceType eq 'Consumption'"
            f" and armRegionName eq '{region}'"
            " and serviceName eq 'Virtual Machines'"
            " and currencyCode eq 'USD'"
            " and contains(skuName,'Spot')"
        ),
    }
    while url:
        items, url = await _fetch_page(client, url, params)
        rows.extend(items)
        params = None
    return rows


async def _fetch_subscription_available(
    candidate_regions: list[str],
) -> tuple[dict[str, set[str]], dict[str, int]]:
    """Return (available, vcpu_counts) for VM sizes with no subscription restrictions.

    available: {region → set of lower-cased vm_size strings} — sizes that pass
      the per-region SKU restriction check (NotAvailableForSubscription / QuotaId).
    vcpu_counts: {lower-cased vm_size → vCPU count} collected from SKU capabilities.

    On any error returns ({}, {}) so the caller skips the filter rather than
    showing a blank page.
    """
    from services.azure_client import compute_client

    available: dict[str, set[str]] = {r: set() for r in candidate_regions}
    region_set_lower = {r.lower(): r for r in candidate_regions}
    vcpu_counts: dict[str, int] = {}

    try:
        async with compute_client() as comp:
            skus = comp.resource_skus.list(
                filter="resourceType eq 'virtualMachines'"
            )
            async for sku in skus:
                if sku.resource_type != "virtualMachines":
                    continue
                vm_size_lower = (sku.name or "").lower()
                if not vm_size_lower:
                    continue

                # Collect vCPU count once per vm_size from SKU capabilities
                if vm_size_lower not in vcpu_counts:
                    for cap in sku.capabilities or []:
                        if cap.name == "vCPUs":
                            try:
                                vcpu_counts[vm_size_lower] = int(cap.value)
                            except (TypeError, ValueError):
                                pass
                            break

                for loc in sku.locations or []:
                    canonical = region_set_lower.get(loc.lower())
                    if not canonical:
                        continue

                    restricted = False
                    for r in sku.restrictions or []:
                        if getattr(r, "type", None) == "Location":
                            reason = getattr(r, "reason_code", None)
                            if reason in ("NotAvailableForSubscription", "QuotaId"):
                                restricted = True
                                break
                    if not restricted:
                        available[canonical].add(vm_size_lower)

        total = sum(len(v) for v in available.values())
        logger.info(
            "Subscription SKU check: %d available (vm_size, region) pairs across %d regions",
            total,
            len(candidate_regions),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Subscription SKU check failed — showing all priced VMs: %s", exc
        )
        return {}, {}

    return available, vcpu_counts




async def _fetch_raw_spot_prices(candidate_regions: list[str]) -> list[dict]:
    """Fetch Spot VM prices for all candidate regions in parallel.

    Fetching per-region and running concurrently reduces total wall time
    from ~100 s (global paginate) to ~5-10 s (parallel regional fetches).
    """

    async with httpx.AsyncClient(timeout=30.0) as client:
        tasks = [_fetch_region(client, r) for r in candidate_regions]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    rows: list[dict] = []
    for region, result in zip(candidate_regions, results):
        if isinstance(result, Exception):
            logger.warning("Failed to fetch prices for %s: %s", region, result)
        else:
            rows.extend(result)

    logger.info(
        "Fetched %d Spot VM price rows across %d regions",
        len(rows),
        len(candidate_regions),
    )
    return rows


def _build_inventory(
    rows: list[dict],
    available_skus: dict[str, set[str]],
    vcpu_counts: dict[str, int],
) -> dict:
    """Build the inventory dict from raw price rows.

    available_skus: {region → set_of_vm_size_lower} from the Resource SKUs API.
    vcpu_counts: {vm_size_lower → vCPU count} from SKU capabilities.

    The SKU filter is skipped when unavailable so the page still shows
    something useful on API errors. Spot quota is NOT filtered here —
    lowPriorityCores is per-region, so the provisioning activity checks it
    per-region at provision time.
    """
    skip_sku_filter = not available_skus

    # vm_size → region → best_price
    pricing: dict[str, dict[str, float]] = {}

    for item in rows:
        vm_size: str = item.get("armSkuName", "").strip()
        region: str = item.get("armRegionName", "").strip()
        price: float = item.get("retailPrice", float("inf"))
        if not vm_size or not region or price <= 0:
            continue

        vm_size_lower = vm_size.lower()

        # Drop (vm_size, region) pairs the subscription can't use per SKU restrictions
        if not skip_sku_filter:
            region_set = available_skus.get(region, set())
            if vm_size_lower not in region_set:
                continue

        bucket = pricing.setdefault(vm_size, {})
        if region not in bucket or price < bucket[region]:
            bucket[region] = price

    items = []
    for vm_size, region_prices in pricing.items():
        gpu = _gpu_label(vm_size)
        sorted_regions = sorted(region_prices.items(), key=lambda x: x[1])
        items.append({
            "vm_size": vm_size,
            "family": _family(vm_size),
            "gpu": gpu,
            "regions": [{"region": r, "price_usd": round(p, 5)} for r, p in sorted_regions],
            "best_price_usd": round(sorted_regions[0][1], 5) if sorted_regions else None,
            "best_region": sorted_regions[0][0] if sorted_regions else None,
            "subscription_verified": not skip_sku_filter,
        })

    # GPU first, then cheapest first
    items.sort(key=lambda x: (0 if x["gpu"] else 1, x["best_price_usd"] or 9999))

    return {
        "items": items,
        "total": len(items),
        "fetched_at": datetime.now(UTC).isoformat(),
    }


@router.get("/inventory/spot-prices")
async def get_spot_inventory(
    family: str | None = Query(None, description="Filter by VM family prefix, e.g. NC, D, E"),
    gpu_only: bool = Query(False, description="Return only GPU-capable VM sizes"),
    region: str | None = Query(None, description="Only include prices for this region"),
    refresh: bool = Query(False, description="Force-refresh the cache"),
) -> dict:
    """Return all Azure VM sizes available as Spot with cheapest price per region.

    Results are cached for 1 hour. Pass ?refresh=true to force a fresh fetch.
    """
    global _cache  # noqa: PLW0603

    now = time.monotonic()
    if _cache is None or refresh or (now - _cache[0]) > _CACHE_TTL:
        try:
            from config import get_settings
            regions = list(get_settings().azure_candidate_regions)
            # Fetch prices and SKU availability in parallel
            rows, (available_skus, vcpu_counts) = await asyncio.gather(
                _fetch_raw_spot_prices(regions),
                _fetch_subscription_available(regions),
            )
            payload = _build_inventory(rows, available_skus, vcpu_counts)
            _cache = (now, payload)
        except Exception as exc:
            logger.error("Failed to fetch Spot prices: %s", exc)
            if _cache:
                # Return stale cache rather than erroring
                logger.warning("Returning stale cache (age=%.0f s)", now - _cache[0])
                payload = _cache[1]
            else:
                raise HTTPException(
                    status_code=503,
                    detail=f"Azure Retail Prices API unavailable: {exc}",
                )
    else:
        payload = _cache[1]

    items = payload["items"]

    # Apply filters
    if family:
        prefix = family.upper()
        items = [i for i in items if i["family"].startswith(prefix)]
    if gpu_only:
        items = [i for i in items if i["gpu"]]
    if region:
        filtered = []
        for i in items:
            region_rows = [r for r in i["regions"] if r["region"] == region]
            if region_rows:
                filtered.append({**i, "regions": region_rows, "best_price_usd": region_rows[0]["price_usd"], "best_region": region})
        items = filtered

    subscription_verified = bool(items) and items[0].get("subscription_verified", False)
    return {
        "items": items,
        "total": len(items),
        "fetched_at": payload["fetched_at"],
        "cached": _cache is not None and not refresh,
        "subscription_verified": subscription_verified,
    }


# ── Bare VM launch ────────────────────────────────────────────────────────────

class _LaunchRequest:
    def __init__(self, vm_size: str, region: str | None):
        self.vm_size = vm_size
        self.region = region


from pydantic import BaseModel  # noqa: E402


class LaunchBareVMRequest(BaseModel):
    vm_size: str
    region: str | None = None


@router.post("/inventory/launch-vm")
async def launch_bare_vm(body: LaunchBareVMRequest, request: FastAPIRequest) -> dict:
    """Start a bare Spot VM (SSH only, no model) via Temporal.

    Creates a pending VMInstance record (model_id/model_name blank) immediately
    so the VM appears in the instances table. The workflow updates it with
    region + IP as it progresses.
    """
    from config import get_settings
    from db.cosmos import get_instances_container
    from db.models import VMInstance, VMStatus
    from temporal.types import LaunchBareVMInput
    from temporal.workflows.vm_provisioning import LaunchBareVMWorkflow

    settings = get_settings()
    short_id = uuid.uuid4().hex[:8]
    vm_name = f"bare-{short_id}"
    workflow_id = f"bare-vm-{short_id}"

    temporal_client = request.app.state.temporal_client

    # Create pending instance record with blank model fields
    instance = VMInstance(
        id=vm_name,
        vm_name=vm_name,
        resource_group=settings.azure_resource_group,
        status=VMStatus.pending,
        workflow_id=workflow_id,
    )
    instances_container = get_instances_container()
    await instances_container.create_item(body=instance.model_dump())

    handle = await temporal_client.start_workflow(
        LaunchBareVMWorkflow.run,
        LaunchBareVMInput(
            vm_name=vm_name,
            resource_group=settings.azure_resource_group,
            vm_size=body.vm_size,
            region=body.region,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )

    logger.info("Started LaunchBareVMWorkflow %s for vm=%s size=%s", handle.id, vm_name, body.vm_size)
    return {
        "vm_name": vm_name,
        "workflow_id": handle.id,
        "vm_size": body.vm_size,
        "region": body.region or "auto",
        "status": "provisioning",
    }


_ACTIVITY_LABELS: dict[str, str] = {
    "get_cheapest_region": "Finding cheapest region...",
    "provision_azure_vm":  "Provisioning VM on Azure...",
    "delete_azure_vm":     "Cleaning up, trying next region...",
    "update_vm_status":    "Updating status...",
    "wait_for_model_ready": "Waiting for model to load...",
}


def _activity_info(pending: list) -> dict | None:
    """Extract current activity details from Temporal pending_activities list."""
    if not pending:
        return None
    act = pending[0]
    name: str = getattr(getattr(act, "activity_type", None), "name", "") or ""
    attempt: int = getattr(act, "attempt", 1)
    state_val = getattr(act, "state", None)
    # state may be an enum or a raw int depending on SDK/protobuf version
    if state_val is None:
        state_str = "UNKNOWN"
    elif hasattr(state_val, "name"):
        state_str = state_val.name
    else:
        state_str = str(state_val)

    last_failure_msg: str | None = None
    lf = getattr(act, "last_failure", None)
    if lf:
        last_failure_msg = getattr(lf, "message", None) or str(lf)

    return {
        "name": name,
        "display": _ACTIVITY_LABELS.get(name, name.replace("_", " ").title() + "..."),
        "attempt": attempt,
        "state": state_str,
        "last_failure": last_failure_msg,
    }


@router.get("/inventory/bare-vms/{workflow_id}")
async def get_bare_vm_status(workflow_id: str, request: FastAPIRequest) -> dict:
    """Poll status of a bare VM launch workflow.

    Returns structured activity info so the frontend can show a live log.
    """
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError

    temporal_client = request.app.state.temporal_client

    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
        wf_status = desc.status

        if wf_status == WorkflowExecutionStatus.COMPLETED:
            result = await handle.result()
            # handle.result() without result_type returns a plain dict from JSON
            if isinstance(result, dict):
                vm_name = result.get("vm_name", "")
                ip_address = result.get("ip_address", "")
                region = result.get("region", "")
            else:
                vm_name = result.vm_name
                ip_address = result.ip_address
                region = result.region
            return {
                "workflow_id": workflow_id,
                "status": "running",
                "vm_name": vm_name,
                "ip_address": ip_address,
                "region": region,
                "current_activity": None,
            }

        if wf_status == WorkflowExecutionStatus.FAILED:
            # Extract the real failure message from the workflow
            error_msg = "Workflow failed"
            try:
                await handle.result()
            except WorkflowFailureError as wfe:
                cause = wfe.__cause__
                error_msg = getattr(cause, "message", None) or str(wfe)
            except Exception as exc:
                error_msg = str(exc)
            return {
                "workflow_id": workflow_id,
                "status": "failed",
                "error": error_msg,
                "current_activity": None,
            }

        if wf_status == WorkflowExecutionStatus.CANCELED:
            return {"workflow_id": workflow_id, "status": "cancelled", "current_activity": None}

        # Still running — expose current activity
        return {
            "workflow_id": workflow_id,
            "status": "provisioning",
            "current_activity": _activity_info(list(desc.raw_description.pending_activities) or []),
        }

    except RPCError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Unexpected error polling workflow %s: %s", workflow_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
