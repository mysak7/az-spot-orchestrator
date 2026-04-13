"""Spot VM price inventory endpoint.

Queries the Azure Retail Prices API for all virtualMachines Spot prices,
groups them by VM size / region, and returns a sorted inventory.
Results are cached in-process for 1 hour to avoid hammering the pricing API.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Query

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


async def _fetch_raw_spot_prices() -> list[dict]:
    """Paginate through Azure Retail Prices API and return all Spot VM rows."""
    rows: list[dict] = []
    params: dict[str, str] = {
        "api-version": "2023-01-01-preview",
        "$filter": (
            "priceType eq 'Consumption'"
            " and serviceName eq 'Virtual Machines'"
            " and currencyCode eq 'USD'"
        ),
    }
    url: str | None = _PRICING_URL

    async with httpx.AsyncClient(timeout=30.0) as client:
        page = 0
        while url:
            resp = await client.get(url, params=params if page == 0 else {})
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("Items", []):
                sku_name: str = item.get("skuName", "")
                if "Spot" not in sku_name and "Low Priority" not in sku_name:
                    continue
                rows.append(item)

            url = data.get("NextPageLink") or None
            page += 1

    logger.info("Fetched %d Spot VM price rows across %d pages", len(rows), page)
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
            rows = await _fetch_raw_spot_prices()
            payload = _build_inventory(rows)
            _cache = (now, payload)
        except Exception as exc:
            logger.error("Failed to fetch Spot prices: %s", exc)
            if _cache:
                # Return stale data rather than erroring
                payload = _cache[1]
            else:
                raise
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
