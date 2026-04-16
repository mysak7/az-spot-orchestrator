"""Azure-specific Temporal activities.

All network / Azure SDK calls live here so the Workflow remains deterministic.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import structlog
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from config import get_settings
from services.azure_client import compute_client, network_client
from temporal.types import (
    DeleteAzureVMInput,
    GetCheapestRegionInput,
    GetCheapestRegionResult,
    ProvisionAzureVMInput,
    WaitForModelInput,
)

log = structlog.get_logger()

_PRICING_URL = "https://prices.azure.com/api/retail/prices"


def _is_arm_vm_size(vm_size: str) -> bool:
    """Return True if the VM size requires an ARM64 OS image.

    Azure ARM (Ampere Altra) sizes have 'p' immediately after the vCPU count,
    e.g. Standard_D2ps_v5, Standard_E4pls_v5, Standard_D8pds_v5.
    """
    return bool(re.search(r"\d+p[a-z]", vm_size, re.IGNORECASE))


async def _filter_sku_available_regions(
    vm_size: str, candidate_regions: list[str]
) -> tuple[list[str], int, str]:
    """Remove regions where vm_size is quota-restricted or not offered.

    Uses the Azure Resource SKUs API to check subscription-level restrictions.
    On any API error the original list is returned unchanged.

    Returns (available_regions, vcpu_count, vm_family) where vcpu_count is the
    number of vCPUs for vm_size (0 if not determinable) and vm_family is the
    Azure family string from SKU capabilities (e.g. 'standardNCASv3_T4Family'),
    used to check family-specific low-priority quota.
    """
    restricted: set[str] = set()
    region_lower: dict[str, str] = {r.lower(): r for r in candidate_regions}
    vcpu_count: int = 0
    vm_family: str = ""

    try:
        async with compute_client() as comp:
            skus = comp.resource_skus.list(filter=f"name eq '{vm_size}'")
            async for sku in skus:
                if sku.name != vm_size or sku.resource_type != "virtualMachines":
                    continue
                # Extract vCPU count and VM family from SKU capabilities (same for all regions)
                if vcpu_count == 0 or not vm_family:
                    for cap in sku.capabilities or []:
                        if cap.name == "vCPUs" and vcpu_count == 0:
                            try:
                                vcpu_count = int(cap.value or 0)
                            except (TypeError, ValueError):
                                pass
                        elif cap.name == "Family" and not vm_family:
                            vm_family = cap.value or ""
                loc_raw: str = (sku.locations or [""])[0] or ""
                matched = region_lower.get(loc_raw.lower())
                if not matched:
                    continue
                for restriction in sku.restrictions or []:
                    reason = getattr(restriction, "reason_code", None)
                    if reason in ("NotAvailableForSubscription", "QuotaId"):
                        restricted.add(matched)
                        log.info(
                            "sku_restricted",
                            vm_size=vm_size,
                            region=matched,
                            reason=reason,
                        )
                        break
    except Exception as exc:  # noqa: BLE001
        log.warning("sku_check_failed", vm_size=vm_size, error=str(exc))
        return candidate_regions, vcpu_count, vm_family

    available = [r for r in candidate_regions if r not in restricted]
    if restricted:
        log.info(
            "sku_filter_complete",
            vm_size=vm_size,
            available_count=len(available),
            removed=sorted(restricted),
        )
    return (available if available else candidate_regions), vcpu_count, vm_family


async def _check_spot_quota(vm_size: str, vcpu_count: int, region: str, vm_family: str = "") -> bool:
    """Return True if this region has enough Spot quota for vcpu_count cores.

    Checks two quota dimensions:
    1. Global lowPriorityCores — per-region Spot vCPU ceiling.
    2. Family-specific low-priority quota (e.g. standardNCASv3_T4FamilyLowPriorityVCPUs)
       — critical for GPU families which have a separate per-family limit that is 0
       by default on pay-as-you-go subscriptions.

    Returns True on any API error so the caller doesn't skip potentially valid regions.
    """
    if vcpu_count == 0:
        log.warning("vcpu_count_unknown", vm_size=vm_size, region=region)
        return True

    # Family prefix used for prefix-matching usage keys, e.g. "standardncasv3_t4family"
    # matches usage keys like "standardncasv3_t4familylowpriority"
    family_lower = vm_family.lower() if vm_family else ""

    try:
        global_ok: bool | None = None
        family_ok: bool | None = None

        async with compute_client() as comp:
            usages = comp.usage.list(location=region)
            async for usage in usages:
                usage_key = (getattr(usage.name, "value", None) or "").lower()
                limit: int = usage.limit or 0
                current: int = usage.current_value or 0
                remaining = limit - current

                if usage_key == "lowprioritycores":
                    log.info(
                        "spot_quota_check",
                        region=region,
                        quota_type="lowPriorityCores",
                        limit=limit,
                        used=current,
                        remaining=remaining,
                        need=vcpu_count,
                    )
                    global_ok = vcpu_count <= remaining

                elif (
                    family_lower
                    and "lowpriority" in usage_key
                    and usage_key != "lowprioritycores"
                    and usage_key.startswith(family_lower)
                ):
                    log.info(
                        "spot_quota_check",
                        region=region,
                        quota_type=usage.name.value,
                        limit=limit,
                        used=current,
                        remaining=remaining,
                        need=vcpu_count,
                    )
                    if limit == 0:
                        log.warning(
                            "spot_quota_zero",
                            vm_family=vm_family,
                            region=region,
                            hint="GPU families require a quota increase on PAYG subscriptions (aka.ms/AzurePortalQuota)",
                        )
                    family_ok = vcpu_count <= remaining

                if global_ok is not None and (not family_lower or family_ok is not None):
                    break  # collected everything we need

        if global_ok is None:
            log.warning("spot_quota_not_found", region=region)

        # Both checks must pass when applicable
        if global_ok is False:
            return False
        if family_ok is False:
            return False
        return True

    except Exception as exc:  # noqa: BLE001
        log.warning("spot_quota_check_failed", region=region, error=str(exc))
    return True  # fail open


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
        from azure.identity.aio import DefaultAzureCredential  # noqa: PLC0415,I001  # local import to avoid top-level cost

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
            log.info("placement_scores", vm_size=vm_size, scores=scores)
        else:
            log.warning(
                "placement_score_api_error",
                vm_size=vm_size,
                status=resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("placement_score_check_failed", vm_size=vm_size, error=str(exc))

    return scores


@activity.defn
async def get_cheapest_region(input: GetCheapestRegionInput) -> GetCheapestRegionResult:
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
    available, vcpu_count, vm_family = await _filter_sku_available_regions(
        input.vm_size, list(input.candidate_regions)
    )
    if vm_family:
        log.info("vm_family_detected", vm_size=input.vm_size, vm_family=vm_family)

    # ── 1b. Per-region Spot quota filter ─────────────────────────────────
    # Checks both the global lowPriorityCores limit and the family-specific
    # low-priority quota (critical for GPU sizes that default to 0 on PAYG subs).
    # If ALL regions report insufficient quota we fall back to the full available
    # list and let the provisioner handle Azure-level rejection per-region.
    if vcpu_count > 0 and available:
        quota_ok = []
        for r in available:
            if await _check_spot_quota(input.vm_size, vcpu_count, r, vm_family):
                quota_ok.append(r)
            else:
                log.info(
                    "region_excluded_quota",
                    region=r,
                    vm_size=input.vm_size,
                    vcpu_count=vcpu_count,
                )
        if quota_ok:
            available = quota_ok
        else:
            log.warning(
                "all_regions_quota_insufficient",
                vm_size=input.vm_size,
                vcpu_count=vcpu_count,
                vm_family=vm_family or "unknown",
            )

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
            log.warning(
                "pricing_api_error",
                vm_size=input.vm_size,
                status=resp.status_code,
            )

    for r, p in prices.items():
        log.info(
            "spot_price",
            vm_size=input.vm_size,
            region=r,
            price_usd=round(p, 4),
            placement_score=scores.get(r),
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

    log.info(
        "region_ranking_complete",
        vm_size=input.vm_size,
        regions=ordered,
        top_region=ordered[0] if ordered else None,
        top_price_usd=round(prices[ordered[0]], 4) if ordered and ordered[0] in prices else None,
        top_score=scores.get(ordered[0]) if ordered else None,
    )
    return GetCheapestRegionResult(regions=ordered, prices=prices)


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
                            # Service endpoint needed for Azure Files NFS mounts
                            "service_endpoints": [
                                {"service": "Microsoft.Storage", "locations": [input.region]}
                            ],
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
        if _is_arm_vm_size(input.vm_size):
            image_sku = "22_04-lts-arm64"
        else:
            # Query SKU HyperVGenerations — Gen2-only sizes need the gen2 image.
            # Falls back to "22_04-lts" (Gen1) on any API error.
            image_sku = "22_04-lts"
            try:
                async for sku in comp.resource_skus.list(filter=f"name eq '{input.vm_size}'"):
                    if sku.name == input.vm_size and sku.resource_type == "virtualMachines":
                        for cap in sku.capabilities or []:
                            if cap.name == "HyperVGenerations":
                                if "V1" not in (cap.value or ""):
                                    image_sku = "22_04-lts-gen2"
                                break
                        break
            except Exception as _sku_err:
                log.warning("hyperv_gen_check_failed", vm_name=input.vm_name, error=str(_sku_err))

        log.info("vm_image_selected", vm_name=input.vm_name, vm_size=input.vm_size, image_sku=image_sku)
        log.info("vm_create_started", vm_name=input.vm_name, region=input.region, vm_size=input.vm_size)

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
                            "sku": image_sku,
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
                log.warning(
                    "vm_create_failed",
                    vm_name=input.vm_name,
                    region=input.region,
                    vm_size=input.vm_size,
                    error_code=err_code,
                )
                raise ApplicationError(
                    f"No Spot capacity for {input.vm_size} in {input.region}: {err_code}",
                    type="SkuNotAvailable",
                    non_retryable=True,
                ) from exc
            raise

        # Fetch the allocated public IP (may differ from creation response)
        pip_info = await net.public_ip_addresses.get(input.resource_group, pip_name)
        ip_address: str = pip_info.ip_address  # type: ignore[assignment]
        log.info("vm_create_succeeded", vm_name=input.vm_name, region=input.region, ip_address=ip_address)
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
            activity.heartbeat({"elapsed": elapsed, "timeout": deadline_s, "ip": input.ip_address})
            try:
                resp = await client.get(f"http://{input.ip_address}:11434/api/tags")
                if resp.status_code == 200:
                    loaded = [m["name"] for m in resp.json().get("models", [])]
                    base = input.model_identifier.split(":")[0]
                    if any(base in name for name in loaded):
                        log.info(
                            "model_ready",
                            model_identifier=input.model_identifier,
                            ip_address=input.ip_address,
                            elapsed_s=elapsed,
                        )
                        return
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
                pass  # VM still booting / Ollama not yet up

            log.info(
                "model_download_polling",
                model_identifier=input.model_identifier,
                ip_address=input.ip_address,
                attempt=elapsed // poll_interval_s,
                elapsed_s=elapsed,
            )
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
                        log.warning("nic_still_reserved", nic_name=name, vm_name=input.vm_name)
                        await asyncio.sleep(180)
                    else:
                        raise

    log.info("vm_deleted", vm_name=input.vm_name)
