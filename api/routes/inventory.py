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
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Query

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


def _build_inventory(rows: list[dict]) -> dict:
    # vm_size → region → best_price
    pricing: dict[str, dict[str, float]] = {}

    for item in rows:
        vm_size: str = item.get("armSkuName", "").strip()
        region: str = item.get("armRegionName", "").strip()
        price: float = item.get("retailPrice", float("inf"))
        if not vm_size or not region or price <= 0:
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
            rows = await _fetch_raw_spot_prices(regions)
            payload = _build_inventory(rows)
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

    return {
        "items": items,
        "total": len(items),
        "fetched_at": payload["fetched_at"],
        "cached": _cache is not None and not refresh,
    }
