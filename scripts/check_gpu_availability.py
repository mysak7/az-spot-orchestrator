"""Check which GPU (and optionally all) VM sizes the subscription can use.

Usage:
  # Check only GPU families (NC, ND, NV, NG) across candidate regions:
  python scripts/check_gpu_availability.py

  # Check a specific family:
  python scripts/check_gpu_availability.py --family D

  # Check every VM size (slow):
  python scripts/check_gpu_availability.py --all

Uses DefaultAzureCredential — same auth as the rest of the app.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Allow running from project root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from azure.identity.aio import DefaultAzureCredential
from azure.mgmt.compute.aio import ComputeManagementClient

GPU_FAMILIES = ("NC", "ND", "NV", "NG")

REGIONS = [
    "eastus",
    "westus2",
    "eastus2",
    "westeurope",
    "northeurope",
    "southeastasia",
    "australiaeast",
    "japaneast",
]


def _bar(available: list[str], all_regions: list[str]) -> str:
    return "".join("█" if r in available else "░" for r in all_regions)


async def check(family_filter: str | None, show_all: bool) -> None:
    print(f"\nAzure subscription GPU/VM availability check")
    print(f"Regions: {', '.join(REGIONS)}\n")

    # available[vm_size][region] = True/False/None (None = not offered)
    offered:   dict[str, dict[str, bool]] = {}  # True = available, False = restricted
    restricted_reasons: dict[str, dict[str, str]] = {}

    async with DefaultAzureCredential() as cred:
        async with ComputeManagementClient(cred, _get_subscription_id()) as comp:
            print("Fetching Resource SKUs from Azure... ", end="", flush=True)
            skus = comp.resource_skus.list(filter="resourceType eq 'virtualMachines'")
            count = 0
            async for sku in skus:
                if sku.resource_type != "virtualMachines":
                    continue
                name: str = sku.name or ""

                # Apply family filter
                import re
                m = re.match(r"Standard_([A-Za-z]+)", name)
                fam = m.group(1).upper() if m else ""

                if not show_all:
                    if family_filter:
                        if not fam.startswith(family_filter.upper()):
                            continue
                    else:
                        if not any(fam.startswith(gf) for gf in GPU_FAMILIES):
                            continue

                for loc in sku.locations or []:
                    region = loc.lower()
                    if region not in REGIONS:
                        continue

                    if name not in offered:
                        offered[name] = {}
                        restricted_reasons[name] = {}

                    # Check restrictions
                    restricted = False
                    reason = ""
                    for r in sku.restrictions or []:
                        if getattr(r, "type", None) == "Location":
                            rc = getattr(r, "reason_code", "")
                            if rc in ("NotAvailableForSubscription", "QuotaId"):
                                restricted = True
                                reason = rc
                                break

                    offered[name][region] = not restricted
                    if reason:
                        restricted_reasons[name][region] = reason

                count += 1

    print(f"done ({count} VM SKUs scanned)\n")

    if not offered:
        print("No matching VM sizes found.")
        return

    # Split into available-in-any and restricted-everywhere
    available_any = {k: v for k, v in offered.items() if any(v.values())}
    restricted_all = {k: v for k, v in offered.items() if not any(v.values())}

    col_w = max((len(k) for k in offered), default=20) + 2

    def header():
        region_abbr = [r[:6] for r in REGIONS]
        print(f"{'VM Size':<{col_w}}", "  ".join(f"{a:>6}" for a in region_abbr))
        print("-" * (col_w + len(REGIONS) * 9))

    print(f"{'═'*60}")
    print(f"  AVAILABLE to this subscription ({len(available_any)} VM sizes)")
    print(f"{'═'*60}")
    header()
    for name in sorted(available_any):
        cells = []
        for region in REGIONS:
            state = available_any[name].get(region)
            if state is True:
                cells.append("\033[32m   ✓  \033[0m")   # green tick
            elif state is False:
                cells.append("\033[31m   ✗  \033[0m")   # red cross
            else:
                cells.append("   —  ")                   # not offered here
        print(f"{name:<{col_w}}", "  ".join(cells))

    if restricted_all:
        print(f"\n{'═'*60}")
        print(f"  NOT AVAILABLE to this subscription ({len(restricted_all)} VM sizes)")
        print(f"{'═'*60}")
        header()
        for name in sorted(restricted_all):
            reasons = restricted_reasons.get(name, {})
            reason_str = next(iter(set(reasons.values())), "restricted")
            print(f"{name:<{col_w}} \033[90m{reason_str}\033[0m")

    print(f"\nLegend:  ✓ = available  ✗ = subscription restricted  — = not offered in region\n")


def _get_subscription_id() -> str:
    from config import get_settings
    sub = get_settings().azure_subscription_id
    if not sub:
        raise SystemExit("AZURE_SUBSCRIPTION_ID not set in .env")
    return sub


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--family", help="Filter to a VM family prefix, e.g. NC, D, E")
    parser.add_argument("--all", action="store_true", dest="show_all", help="Check all VM sizes (slow)")
    args = parser.parse_args()

    asyncio.run(check(args.family, args.show_all))
