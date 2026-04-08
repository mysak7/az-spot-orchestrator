"""Azure-specific Temporal activities.

All network / Azure SDK calls live here so the Workflow remains deterministic.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from azure.core.exceptions import ResourceNotFoundError
from temporalio import activity

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


@activity.defn
async def get_cheapest_region(input: GetCheapestRegionInput) -> str:
    """Query Azure Retail Prices API and return the candidate region with the lowest Spot price."""
    prices: dict[str, float] = {}

    params = {
        "api-version": "2023-01-01-preview",
        "$filter": (
            f"priceType eq 'Spot' and armSkuName eq '{input.vm_size}'"
            f" and currencyCode eq 'USD'"
        ),
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(_PRICING_URL, params=params)
        resp.raise_for_status()
        for item in resp.json().get("Items", []):
            region: str = item.get("armRegionName", "")
            price: float = item.get("retailPrice", float("inf"))
            if region in input.candidate_regions:
                if region not in prices or price < prices[region]:
                    prices[region] = price

    if not prices:
        logger.warning(
            "No spot pricing found for %s; defaulting to %s",
            input.vm_size,
            input.candidate_regions[0],
        )
        return input.candidate_regions[0]

    cheapest = min(prices, key=lambda r: prices[r])
    logger.info("Cheapest region for %s: %s ($%.4f/hr)", input.vm_size, cheapest, prices[cheapest])
    return cheapest


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
        nsg_poller = await net.network_security_groups.begin_create_or_update(
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
            vnet_poller = await net.virtual_networks.begin_create_or_update(
                input.resource_group,
                vnet_name,
                {
                    "location": input.region,
                    "address_space": {"address_prefixes": ["10.0.0.0/16"]},
                    "subnets": [
                        {
                            "name": subnet_name,
                            "properties": {
                                "address_prefix": "10.0.0.0/24",
                                "network_security_group": {"id": nsg.id},
                            },
                        }
                    ],
                },
            )
            vnet = await vnet_poller.result()
            subnet = (vnet.subnets or [])[0]

        # ── Public IP ─────────────────────────────────────────────────────
        pip_poller = await net.public_ip_addresses.begin_create_or_update(
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
        nic_poller = await net.network_interfaces.begin_create_or_update(
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
        vm_poller = await comp.virtual_machines.begin_create_or_update(
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
                        "managed_disk": {"storage_account_type": "Premium_LRS"},
                        "delete_option": "Delete",
                    },
                },
                "os_profile": {
                    "computer_name": input.vm_name[:15],
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
                "network_profile": {
                    "network_interfaces": [{"id": nic.id, "primary": True}]
                },
                # Spot configuration
                "priority": "Spot",
                "eviction_policy": "Deallocate",
                "billing_profile": {"max_price": -1},  # pay market rate
            },
        )
        await vm_poller.result()

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
                resp = await client.get(
                    f"http://{input.ip_address}:11434/api/tags"
                )
                if resp.status_code == 200:
                    loaded = [m["name"] for m in resp.json().get("models", [])]
                    base = input.model_identifier.split(":")[0]
                    if any(base in name for name in loaded):
                        logger.info("Model %s ready on %s", input.model_identifier, input.ip_address)
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
            poller = await comp.virtual_machines.begin_delete(
                input.resource_group, input.vm_name
            )
            await poller.result()
        except ResourceNotFoundError:
            pass

        for delete_fn, name in [
            (net.network_interfaces.begin_delete, nic_name),
            (net.public_ip_addresses.begin_delete, pip_name),
        ]:
            try:
                poller = await delete_fn(input.resource_group, name)
                await poller.result()
            except ResourceNotFoundError:
                pass

    logger.info("Deleted VM %s and associated resources", input.vm_name)
