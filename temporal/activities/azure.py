"""Azure-specific Temporal activities.

All network / Azure SDK calls live here so the Workflow remains deterministic.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from config import get_settings
from services.azure_client import compute_client, network_client
from temporal.types import (
    DeleteAzureVMInput,
    GetCheapestRegionInput,
    ProvisionAzureVMInput,
    WaitForModelInput,
)

logger = logging.getLogger(__name__)

_PRICING_URL = "https://prices.azure.com/api/retail/prices"


async def _filter_sku_available_regions(
    vm_size: str, candidate_regions: list[str]
) -> tuple[list[str], int]:
    """Remove regions where vm_size is quota-restricted or not offered.

    Uses the Azure Resource SKUs API to check subscription-level restrictions.
    On any API error the original list is returned unchanged.

    Returns (available_regions, vcpu_count) where vcpu_count is the number of
    vCPUs for vm_size (0 if not determinable).
    """
    restricted: set[str] = set()
    region_lower: dict[str, str] = {r.lower(): r for r in candidate_regions}
    vcpu_count: int = 0

    try:
        async with compute_client() as comp:
            skus = comp.resource_skus.list(filter=f"name eq '{vm_size}'")
            async for sku in skus:
                if sku.name != vm_size or sku.resource_type != "virtualMachines":
                    continue
                # Extract vCPU count from SKU capabilities (same for all regions)
                if vcpu_count == 0:
                    for cap in sku.capabilities or []:
                        if cap.name == "vCPUs":
                            try:
                                vcpu_count = int(cap.value)
                            except (TypeError, ValueError):
                                pass
                            break
                loc_raw: str = (sku.locations or [None])[0] or ""
                matched = region_lower.get(loc_raw.lower())
                if not matched:
                    continue
                for restriction in sku.restrictions or []:
                    reason = getattr(restriction, "reason_code", None)
                    if reason in ("NotAvailableForSubscription", "QuotaId"):
                        restricted.add(matched)
                        logger.info(
                            "SKU %s restricted in %s (reason: %s) — excluding",
                            vm_size,
                            matched,
                            reason,
                        )
                        break
    except Exception as exc:  # noqa: BLE001
        logger.warning("SKU availability check failed, using all regions: %s", exc)
        return candidate_regions, vcpu_count

    available = [r for r in candidate_regions if r not in restricted]
    if restricted:
        logger.info(
            "Regions after SKU filter: %s (removed: %s)", available, sorted(restricted)
        )
    return (available if available else candidate_regions), vcpu_count


async def _check_spot_quota(vm_size: str, vcpu_count: int, region: str) -> None:
    """Raise InsufficientSpotQuota if the subscription cannot fit vcpu_count Spot cores.

    lowPriorityCores is a subscription-wide limit — Azure does not reflect it in
    per-region SKU restrictions, so it is invisible to _filter_sku_available_regions.
    Checking it here prevents the workflow from cycling through every candidate region
    only to get OperationNotAllowed on each.

    On any API error the check is skipped (fail open).
    """
    if vcpu_count == 0:
        logger.warning("vCPU count unknown for %s — skipping Spot quota pre-check", vm_size)
        return

    try:
        async with compute_client() as comp:
            usages = comp.usage.list(location=region)
            async for usage in usages:
                if (getattr(usage.name, "value", None) or "").lower() == "lowprioritycores":
                    limit: int = usage.limit or 0
                    current: int = usage.current_value or 0
                    remaining = limit - current
                    logger.info(
                        "Spot quota (lowPriorityCores): limit=%d used=%d remaining=%d, need=%d",
                        limit,
                        current,
                        remaining,
                        vcpu_count,
                    )
                    if vcpu_count > remaining:
                        raise ApplicationError(
                            f"Insufficient Spot quota: {vm_size} needs {vcpu_count} vCPUs "
                            f"but only {remaining} lowPriorityCores remain "
                            f"(limit={limit}, used={current}). "
                            "Request a quota increase at aka.ms/AzurePortalQuota.",
                            type="InsufficientSpotQuota",
                            non_retryable=True,
                        )
                    return
        logger.warning("LowPriorityCores usage not found in region %s — skipping quota check", region)
    except ApplicationError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spot quota pre-check failed, proceeding anyway: %s", exc)


async def _get_spot_placement_scores(
    vm_size: str, candidate_regions: list[str], http_client: httpx.AsyncClient
) -> dict[str, int]:
    """Call the Spot Placement Score API for each region.

    Returns a dict mapping region → score (0–5, higher is better capacity signal).
    On any error returns an empty dict so the caller falls back to price ordering.
    """
    settings = get_settings()
    scores: dict[str, int] = {}

    # Batch all regions in a single call per the API spec
    url = (
        f"https://management.azure.com/subscriptions/{settings.azure_subscription_id}"
        "/providers/Microsoft.Compute/locations/global/spotPlacementScores"
        "?api-version=2024-03-01-preview"
    )
    body = {
        "desiredLocations": candidate_regions,
        "desiredSizes": [{"sku": vm_size}],
        "desiredCount": 1,
        "availabilityZones": False,
    }

    try:
        from azure.identity.aio import DefaultAzureCredential  # local import to avoid top-level cost

        async with DefaultAzureCredential() as cred:
            token_obj = await cred.get_token("https://management.azure.com/.default")
        token = token_obj.token

        resp = await http_client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        if resp.status_code == 200:
            for entry in resp.json().get("placementScores", []):
                region: str = entry.get("region", "")
                score: int = entry.get("score", 0)
                if region in candidate_regions:
                    scores[region] = score
            logger.info("Spot placement scores for %s: %s", vm_size, scores)
        else:
            logger.warning(
                "Spot Placement Score API returned %s — falling back to price order",
                resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spot placement score check failed: %s", exc)

    return scores


@activity.defn
async def get_cheapest_region(input: GetCheapestRegionInput) -> list[str]:
    """Return candidate regions ordered by Spot viability.

    Pipeline:
      1. Filter out regions where the SKU has quota/subscription restrictions.
      2. Fetch Spot Placement Scores (capacity signal, 0-5) — skipped on error.
      3. Fetch Spot prices from the Azure Retail Prices API.
      4. Sort by: (score desc, price asc); regions without pricing appended last.

    Regions with no data are appended at the end so the caller can still fall
    back to them.
    """
    # ── 1. SKU availability filter + vCPU count ──────────────────────────
    available, vcpu_count = await _filter_sku_available_regions(
        input.vm_size, list(input.candidate_regions)
    )

    # ── 1b. Subscription-wide Spot quota pre-check ────────────────────────
    # lowPriorityCores is a global limit not reflected in SKU restrictions.
    # Check it early so we fail fast instead of exhausting all regions.
    if available:
        await _check_spot_quota(input.vm_size, vcpu_count, available[0])

    prices: dict[str, float] = {}
    scores: dict[str, int] = {}

    params = {
        "api-version": "2023-01-01-preview",
        "$filter": (
            f"priceType eq 'Consumption' and armSkuName eq '{input.vm_size}'"
            f" and currencyCode eq 'USD'"
        ),
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # ── 2. Spot placement scores (capacity signal) ────────────────────
        scores = await _get_spot_placement_scores(input.vm_size, available, client)

        # ── 3. Spot prices ────────────────────────────────────────────────
        resp = await client.get(_PRICING_URL, params=params)
        if resp.status_code == 200:
            for item in resp.json().get("Items", []):
                sku_name: str = item.get("skuName", "")
                if "Spot" not in sku_name and "Low Priority" not in sku_name:
                    continue  # skip on-demand / reservation rows
                region: str = item.get("armRegionName", "")
                price: float = item.get("retailPrice", float("inf"))
                if region in available:
                    if region not in prices or price < prices[region]:
                        prices[region] = price
        else:
            logger.warning(
                "Azure Pricing API returned %s for %s",
                resp.status_code,
                input.vm_size,
            )

    for r, p in prices.items():
        logger.info(
            "Spot price for %s in %s: $%.4f/hr (score=%s)",
            input.vm_size,
            r,
            p,
            scores.get(r, "n/a"),
        )

    # ── 4. Sort: score desc, price asc ───────────────────────────────────
    priced = sorted(
        prices,
        key=lambda r: (-scores.get(r, 0), prices[r]),
    )
    unpriced = sorted(
        [r for r in available if r not in prices],
        key=lambda r: -scores.get(r, 0),
    )
    ordered = priced + unpriced

    if not ordered:
        ordered = available or list(input.candidate_regions)

    logger.info("Region order for %s: %s", input.vm_size, ordered)
    return ordered


@activity.defn
async def provision_azure_vm(input: ProvisionAzureVMInput) -> str:
    """Provision a Spot VM on Azure and return its public IP address.

    Creates shared per-region VNet/NSG (idempotent) then provisions:
    public IP → NIC → Spot VM with Ollama cloud-init.
    """
    settings = get_settings()

    if not settings.azure_ssh_public_key:
        raise ValueError("AZURE_SSH_PUBLIC_KEY must be set in environment")

    vnet_name = f"az-spot-vnet-{input.region}"
    subnet_name = f"az-spot-subnet-{input.region}"
    nsg_name = f"az-spot-nsg-{input.region}"
    pip_name = f"{input.vm_name}-pip"
    nic_name = f"{input.vm_name}-nic"

    async with network_client() as net, compute_client() as comp:
        # ── NSG (shared per region) ────────────────────────────────────────
        nsg_poller = await net.network_security_groups.begin_create_or_update(  # type: ignore[call-overload]
            input.resource_group,
            nsg_name,
            {
                "location": input.region,
                "security_rules": [
                    {
                        "name": "allow-ollama",
                        "properties": {
                            "priority": 100,
                            "protocol": "TCP",
                            "access": "Allow",
                            "direction": "Inbound",
                            "sourceAddressPrefix": "*",
                            "sourcePortRange": "*",
                            "destinationAddressPrefix": "*",
                            "destinationPortRange": "11434",
                        },
                    },
                    {
                        "name": "allow-ssh",
                        "properties": {
                            "priority": 110,
                            "protocol": "TCP",
                            "access": "Allow",
                            "direction": "Inbound",
                            "sourceAddressPrefix": "*",
                            "sourcePortRange": "*",
                            "destinationAddressPrefix": "*",
                            "destinationPortRange": "22",
                        },
                    },
                ],
            },
        )
        nsg = await nsg_poller.result()

        # ── VNet / Subnet (shared per region, idempotent) ─────────────────
        try:
            vnet = await net.virtual_networks.get(input.resource_group, vnet_name)
            subnet = next(s for s in (vnet.subnets or []) if s.name == subnet_name)
        except (ResourceNotFoundError, StopIteration):
            vnet_poller = await net.virtual_networks.begin_create_or_update(  # type: ignore[call-overload]
                input.resource_group,
                vnet_name,
                {
                    "location": input.region,
                    "address_space": {"address_prefixes": ["10.0.0.0/16"]},
                    "subnets": [
                        {
                            "name": subnet_name,
                            "address_prefix": "10.0.0.0/24",
                            "network_security_group": {"id": nsg.id},
                        }
                    ],
                },
            )
            vnet = await vnet_poller.result()
            subnet = (vnet.subnets or [])[0]

        # ── Public IP ─────────────────────────────────────────────────────
        pip_poller = await net.public_ip_addresses.begin_create_or_update(  # type: ignore[call-overload]
            input.resource_group,
            pip_name,
            {
                "location": input.region,
                "sku": {"name": "Standard"},
                "public_ip_allocation_method": "Static",
            },
        )
        pip = await pip_poller.result()

        # ── NIC ───────────────────────────────────────────────────────────
        nic_poller = await net.network_interfaces.begin_create_or_update(  # type: ignore[call-overload]
            input.resource_group,
            nic_name,
            {
                "location": input.region,
                "ip_configurations": [
                    {
                        "name": "ipconfig1",
                        "subnet": {"id": subnet.id},
                        "public_ip_address": {"id": pip.id},
                    }
                ],
            },
        )
        nic = await nic_poller.result()

        # ── Spot VM ───────────────────────────────────────────────────────
        try:
            vm_poller = await comp.virtual_machines.begin_create_or_update(  # type: ignore[call-overload]
                input.resource_group,
                input.vm_name,
                {
                    "location": input.region,
                    "hardware_profile": {"vm_size": input.vm_size},
                    "storage_profile": {
                        "image_reference": {
                            "publisher": "Canonical",
                            "offer": "0001-com-ubuntu-server-jammy",
                            "sku": "22_04-lts",
                            "version": "latest",
                        },
                        "os_disk": {
                            "create_option": "FromImage",
                            "managed_disk": {"storage_account_type": "StandardSSD_LRS"},
                            "delete_option": "Delete",
                        },
                    },
                    "os_profile": {
                        "computer_name": input.vm_name[:15].rstrip("-"),
                        "admin_username": "azureuser",
                        "linux_configuration": {
                            "disable_password_authentication": True,
                            "ssh": {
                                "public_keys": [
                                    {
                                        "path": "/home/azureuser/.ssh/authorized_keys",
                                        "key_data": settings.azure_ssh_public_key,
                                    }
                                ]
                            },
                        },
                        "custom_data": input.cloud_init_b64,
                    },
                    "network_profile": {"network_interfaces": [{"id": nic.id, "primary": True}]},
                    # Spot configuration
                    "priority": "Spot",
                    "eviction_policy": "Deallocate",
                    "billing_profile": {"max_price": -1},  # pay market rate
                },
            )
            await vm_poller.result()
        except (ResourceExistsError, HttpResponseError) as exc:
            err_code = getattr(exc.error, "code", None) if hasattr(exc, "error") else None
            # Treat both capacity-unavailable and quota-exceeded as "try next region"
            if err_code in ("SkuNotAvailable", "OperationNotAllowed"):
                raise ApplicationError(
                    f"No Spot capacity for {input.vm_size} in {input.region}: {err_code}",
                    type="SkuNotAvailable",
                    non_retryable=True,
                ) from exc
            raise

        # Fetch the allocated public IP (may differ from creation response)
        pip_info = await net.public_ip_addresses.get(input.resource_group, pip_name)
        ip_address: str = pip_info.ip_address  # type: ignore[assignment]
        logger.info("VM %s provisioned at %s", input.vm_name, ip_address)
        return ip_address


@activity.defn
async def wait_for_model_ready(input: WaitForModelInput) -> None:
    """Poll the VM's Ollama endpoint until the target model is downloaded.

    Sends Temporal heartbeats every poll interval so the workflow knows
    the activity is still making progress during long model downloads.
    """
    deadline_s = input.timeout_minutes * 60
    poll_interval_s = 30
    elapsed = 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        while elapsed < deadline_s:
            activity.heartbeat(f"elapsed={elapsed}s ip={input.ip_address}")
            try:
                resp = await client.get(f"http://{input.ip_address}:11434/api/tags")
                if resp.status_code == 200:
                    loaded = [m["name"] for m in resp.json().get("models", [])]
                    base = input.model_identifier.split(":")[0]
                    if any(base in name for name in loaded):
                        logger.info(
                            "Model %s ready on %s", input.model_identifier, input.ip_address
                        )
                        return
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
                pass  # VM still booting / Ollama not yet up

            await asyncio.sleep(poll_interval_s)
            elapsed += poll_interval_s

    raise TimeoutError(
        f"Model '{input.model_identifier}' not ready after {input.timeout_minutes} minutes"
    )


@activity.defn
async def delete_azure_vm(input: DeleteAzureVMInput) -> None:
    """Delete a Spot VM and its associated NIC and public IP."""
    pip_name = f"{input.vm_name}-pip"
    nic_name = f"{input.vm_name}-nic"

    async with compute_client() as comp, network_client() as net:
        # Delete VM first (NIC/PIP cannot be deleted while attached)
        try:
            poller = await comp.virtual_machines.begin_delete(input.resource_group, input.vm_name)
            await poller.result()
        except ResourceNotFoundError:
            pass

        for delete_fn, name in [
            (net.network_interfaces.begin_delete, nic_name),  # type: ignore[list-item]
            (net.public_ip_addresses.begin_delete, pip_name),  # type: ignore[list-item]
        ]:
            for _attempt in range(4):
                try:
                    poller = await delete_fn(input.resource_group, name)
                    await poller.result()
                    break
                except ResourceNotFoundError:
                    break
                except Exception as exc:
                    if "NicReservedForAnotherVm" in str(exc):
                        logger.warning("NIC %s is still reserved; waiting 180 s before retry", name)
                        await asyncio.sleep(180)
                    else:
                        raise

    logger.info("Deleted VM %s and associated resources", input.vm_name)
