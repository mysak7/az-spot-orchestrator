"""Temporal activities for Azure Files NFS share infrastructure and model seeding.

Two activities:
  1. ensure_files_infrastructure  — creates FileStorage account + NFS share + network rules
  2. seed_files_from_blob         — extracts tar.lz4 blob and uploads model files to share
"""

from __future__ import annotations

import asyncio
import lz4.frame
import os
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
import structlog
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.mgmt.storage.models import (
    DefaultAction,
    FileShare,
    IPRule,
    Kind,
    NetworkRuleSet,
    Sku,
    SkuName,
    StorageAccountCreateParameters,
    VirtualNetworkRule,
)
from temporalio import activity
from temporalio.exceptions import ApplicationError

from config import get_settings
from services.files_cache import (
    files_account_name,
    mark_files_available,
    mark_files_failed,
    mark_files_provisioning,
)
from services.files_client import files_service_client, storage_mgmt_client
from services.model_cache import get_blob_read_url
from temporal.types import (
    CheckFilesShareInput,
    CheckFilesShareResult,
    EnsureFilesInfraInput,
    EnsureFilesInfraResult,
    SeedFilesInput,
    SeedFilesResult,
)

log = structlog.get_logger()


async def _get_or_create_subnet_with_storage_endpoint(
    region: str, resource_group: str
) -> str:
    """Return subnet resource ID, ensuring it has the Microsoft.Storage service endpoint.

    Creates the VNet + subnet if they don't exist (same layout as provision_azure_vm).
    Updates the subnet if it exists but lacks the service endpoint.
    """
    from azure.mgmt.network.models import ServiceEndpointPropertiesFormat
    from services.azure_client import compute_client, network_client

    vnet_name = f"az-spot-vnet-{region}"
    subnet_name = f"az-spot-subnet-{region}"
    nsg_name = f"az-spot-nsg-{region}"

    storage_endpoint = {"service": "Microsoft.Storage", "locations": [region]}

    async with network_client() as net, compute_client() as _comp:
        # Ensure NSG exists
        try:
            nsg = await net.network_security_groups.get(resource_group, nsg_name)
        except ResourceNotFoundError:
            nsg_poller = await net.network_security_groups.begin_create_or_update(
                resource_group,
                nsg_name,
                {
                    "location": region,
                    "security_rules": [
                        {
                            "name": "allow-ollama",
                            "properties": {
                                "priority": 100, "protocol": "TCP", "access": "Allow",
                                "direction": "Inbound", "sourceAddressPrefix": "*",
                                "sourcePortRange": "*", "destinationAddressPrefix": "*",
                                "destinationPortRange": "11434",
                            },
                        },
                        {
                            "name": "allow-ssh",
                            "properties": {
                                "priority": 110, "protocol": "TCP", "access": "Allow",
                                "direction": "Inbound", "sourceAddressPrefix": "*",
                                "sourcePortRange": "*", "destinationAddressPrefix": "*",
                                "destinationPortRange": "22",
                            },
                        },
                    ],
                },
            )
            nsg = await nsg_poller.result()

        # Get or create VNet/subnet
        try:
            vnet = await net.virtual_networks.get(resource_group, vnet_name)
            subnet = next((s for s in (vnet.subnets or []) if s.name == subnet_name), None)
            if subnet is None:
                raise ResourceNotFoundError("subnet not found")

            # Check if service endpoint already present
            existing_endpoints = [
                ep.service for ep in (subnet.service_endpoints or [])
            ]
            if "Microsoft.Storage" not in existing_endpoints:
                log.info("storage_endpoint_adding", subnet=subnet_name)
                endpoints = list(subnet.service_endpoints or []) + [
                    ServiceEndpointPropertiesFormat(service="Microsoft.Storage", locations=[region])
                ]
                subnet_poller = await net.subnets.begin_create_or_update(
                    resource_group,
                    vnet_name,
                    subnet_name,
                    {
                        "address_prefix": subnet.address_prefix,
                        "network_security_group": {"id": nsg.id},
                        "service_endpoints": [
                            {"service": ep.service, "locations": ep.locations or [region]}
                            for ep in endpoints
                        ],
                    },
                )
                subnet = await subnet_poller.result()

        except (ResourceNotFoundError, StopIteration):
            vnet_poller = await net.virtual_networks.begin_create_or_update(
                resource_group,
                vnet_name,
                {
                    "location": region,
                    "address_space": {"address_prefixes": ["10.0.0.0/16"]},
                    "subnets": [
                        {
                            "name": subnet_name,
                            "address_prefix": "10.0.0.0/24",
                            "network_security_group": {"id": nsg.id},
                            "service_endpoints": [storage_endpoint],
                        }
                    ],
                },
            )
            vnet = await vnet_poller.result()
            subnet = (vnet.subnets or [])[0]

    return subnet.id  # type: ignore[return-value]


@activity.defn
async def ensure_files_infrastructure(input: EnsureFilesInfraInput) -> EnsureFilesInfraResult:
    """Create (or verify) the FileStorage account + NFS share for a region.

    Idempotent — safe to call multiple times.  Configures:
      - FileStorage Premium_LRS account (NFS 4.1 capable)
      - VNet service endpoint on the region's subnet
      - Network rules: allow subnet + control plane IP
      - NFS share named "models"
    """
    s = get_settings()
    account = files_account_name(input.region)
    share_name = s.azure_files_share_name

    activity.heartbeat(f"ensuring VNet service endpoint in {input.region}")
    subnet_id = await _get_or_create_subnet_with_storage_endpoint(input.region, input.resource_group)
    log.info("storage_endpoint_ready", subnet_id=subnet_id, region=input.region)

    activity.heartbeat(f"creating FileStorage account {account}")
    async with storage_mgmt_client() as mgmt:
        # Create or update storage account
        try:
            existing = await mgmt.storage_accounts.get_properties(input.resource_group, account)
            log.info("files_account_exists", account=account, region=input.region)
        except ResourceNotFoundError:
            existing = None

        if existing is None:
            params = StorageAccountCreateParameters(
                sku=Sku(name=SkuName.PREMIUM_LRS),
                kind=Kind.FILE_STORAGE,
                location=input.region,
                enable_https_traffic_only=True,  # SMB uses HTTPS
                network_rule_set=NetworkRuleSet(
                    default_action=DefaultAction.DENY,
                    bypass="AzureServices",
                    virtual_network_rules=[
                        VirtualNetworkRule(
                            virtual_network_resource_id=subnet_id,
                            action="Allow",
                        )
                    ],
                ),
            )
            poller = await mgmt.storage_accounts.begin_create(input.resource_group, account, params)
            await poller.result()
            log.info("files_account_created", account=account, region=input.region)
        else:
            # Ensure HTTPS-only is enabled (SMB requires HTTPS; old accounts may have it disabled for NFS)
            if not existing.enable_https_traffic_only:
                from azure.mgmt.storage.models import StorageAccountUpdateParameters
                await mgmt.storage_accounts.update(
                    input.resource_group, account,
                    StorageAccountUpdateParameters(enable_https_traffic_only=True),
                )
                log.info("files_account_https_enabled", account=account)

        # Update network rules: allow subnet (VNet rule) + control plane IP
        # We do this as a separate update after creation to avoid race conditions
        activity.heartbeat(f"configuring network rules on {account}")
        try:
            from azure.mgmt.storage.models import StorageAccountUpdateParameters
            await mgmt.storage_accounts.update(
                input.resource_group,
                account,
                StorageAccountUpdateParameters(
                    network_rule_set=NetworkRuleSet(
                        default_action=DefaultAction.DENY,  # NFS requires Deny+explicit rules
                        bypass="AzureServices",
                        virtual_network_rules=[
                            VirtualNetworkRule(
                                virtual_network_resource_id=subnet_id,
                                action="Allow",
                            )
                        ],
                        ip_rules=[
                            IPRule(
                                ip_address_or_range=s.control_plane_public_ip,
                                action="Allow",
                            )
                        ],
                    )
                ),
            )
            log.info("files_network_rules_set", account=account)
        except Exception as exc:
            log.warning("files_network_rules_failed", account=account, error=str(exc))

        # Get storage account key for share operations
        activity.heartbeat(f"getting storage account key for {account}")
        keys_result = await mgmt.storage_accounts.list_keys(input.resource_group, account)
        account_key = (keys_result.keys or [])[0].value
        if not account_key:
            raise ApplicationError(f"Could not retrieve key for storage account {account}")

        # Create SMB share (REST API seeding requires SMB; NFS shares don't support REST writes)
        activity.heartbeat(f"creating SMB share '{share_name}' on {account}")
        try:
            await mgmt.file_shares.create(
                input.resource_group,
                account,
                share_name,
                FileShare(share_quota=5120),  # SMB by default (no enabled_protocols = SMB)
            )
            log.info("smb_share_created", share_name=share_name, account=account, region=input.region)
        except ResourceExistsError:
            # Check if existing share is a leftover NFS share — must replace with SMB
            existing_share = await mgmt.file_shares.get(input.resource_group, account, share_name)
            if existing_share.enabled_protocols and "NFS" in existing_share.enabled_protocols:
                log.info("nfs_share_replacing_with_smb", share_name=share_name, account=account)
                # Disable soft-delete so the deleted NFS share isn't restored on recreate
                from azure.mgmt.storage.models import DeleteRetentionPolicy, FileServiceProperties
                await mgmt.file_services.set_service_properties(
                    input.resource_group, account,
                    FileServiceProperties(
                        share_delete_retention_policy=DeleteRetentionPolicy(enabled=False)
                    ),
                )
                await mgmt.file_shares.delete(input.resource_group, account, share_name)
                # Azure deletes shares async — retry until ShareBeingDeleted clears
                for _attempt in range(12):
                    activity.heartbeat("waiting for NFS share deletion to complete")
                    await asyncio.sleep(5)
                    try:
                        await mgmt.file_shares.create(
                            input.resource_group, account, share_name,
                            FileShare(share_quota=5120),
                        )
                        log.info("smb_share_created_after_nfs_replace", share_name=share_name, account=account)
                        break
                    except Exception as exc:
                        if "ShareBeingDeleted" in str(exc):
                            continue
                        raise
            else:
                log.info("smb_share_exists", share_name=share_name, account=account)

    return EnsureFilesInfraResult(
        storage_account=account,
        share_name=share_name,
        subnet_id=subnet_id,
    )


async def _upload_dir_to_share(
    share_client: object,
    local_dir: Path,
    remote_prefix: str = "",
) -> int:
    """Recursively upload a local directory tree to an Azure Files share.

    Returns total bytes uploaded.
    """
    from azure.core.exceptions import ResourceExistsError

    total_bytes = 0

    async def _ensure_dir(path: str) -> None:
        """Create all directory components of path."""
        parts = [p for p in path.split("/") if p]
        for i in range(len(parts)):
            dir_path = "/".join(parts[: i + 1])
            dir_client = share_client.get_directory_client(dir_path)  # type: ignore[union-attr]
            try:
                await dir_client.create_directory()
            except ResourceExistsError:
                pass

    for root, dirs, files in os.walk(local_dir):
        root_path = Path(root)
        rel_root = root_path.relative_to(local_dir)

        # Build remote directory path
        if remote_prefix:
            remote_dir = f"{remote_prefix}/{rel_root}" if str(rel_root) != "." else remote_prefix
        else:
            remote_dir = str(rel_root) if str(rel_root) != "." else ""

        if remote_dir:
            await _ensure_dir(remote_dir)

        for filename in files:
            local_file = root_path / filename
            if remote_dir:
                remote_file_path = f"{remote_dir}/{filename}"
                dir_client = share_client.get_directory_client(remote_dir)  # type: ignore[union-attr]
            else:
                remote_file_path = filename
                dir_client = share_client.get_directory_client("")  # root  # type: ignore[union-attr]

            file_size = local_file.stat().st_size
            activity.heartbeat(f"uploading {remote_file_path} ({file_size // 1024} KB)")

            file_client = dir_client.get_file_client(filename)
            with open(local_file, "rb") as fh:
                await file_client.upload_file(fh, length=file_size)

            total_bytes += file_size
            log.debug("file_uploaded", path=remote_file_path, size_bytes=file_size)

    return total_bytes


@activity.defn
async def seed_files_from_blob(input: SeedFilesInput) -> SeedFilesResult:
    """Download the model tar.lz4 blob and extract it to the region's NFS share.

    The blob layout (models/blobs/... + models/manifests/...) is preserved on the
    share with the 'models/' prefix stripped, so OLLAMA_MODELS can point directly
    at the share mount.
    """
    s = get_settings()
    account = files_account_name(input.region)
    share_name = s.azure_files_share_name

    start = time.monotonic()

    try:
        # ── Get blob download URL ──────────────────────────────────────────────
        activity.heartbeat("generating blob SAS URL")
        download_url = await get_blob_read_url(input.model_identifier, input.region)

        # ── Get storage account key ────────────────────────────────────────────
        activity.heartbeat("getting storage account key")
        async with storage_mgmt_client() as mgmt:
            keys_result = await mgmt.storage_accounts.list_keys(input.resource_group, account)
            account_key = (keys_result.keys or [])[0].value
        if not account_key:
            raise ApplicationError(f"Could not retrieve key for storage account {account}")

        await mark_files_provisioning(input.model_identifier, input.region, account_key)

        # ── Download + extract to temp dir ─────────────────────────────────────
        with tempfile.TemporaryDirectory(prefix="ollama_files_") as tmpdir:
            tmp_path = Path(tmpdir)
            archive_path = tmp_path / "model.tar.lz4"

            activity.heartbeat("downloading blob archive")
            log.info("nfs_seed_started", model_identifier=input.model_identifier, region=input.region)

            async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as http:
                async with http.stream("GET", download_url) as resp:
                    resp.raise_for_status()
                    with open(archive_path, "wb") as f:
                        downloaded = 0
                        last_hb = time.monotonic()
                        async for chunk in resp.aiter_bytes(chunk_size=4 * 1024 * 1024):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if time.monotonic() - last_hb >= 30:
                                activity.heartbeat(f"downloaded {downloaded // 1_048_576} MB")
                                last_hb = time.monotonic()

            archive_size = archive_path.stat().st_size
            log.info("blob_downloaded", model_identifier=input.model_identifier, size_bytes=archive_size)

            # ── Extract lz4+tar ────────────────────────────────────────────────
            activity.heartbeat("extracting archive")
            extract_dir = tmp_path / "extracted"
            extract_dir.mkdir()

            with lz4.frame.open(str(archive_path), "rb") as lz4_fh:
                with tarfile.open(fileobj=lz4_fh, mode="r|") as tar:  # type: ignore[arg-type]
                    tar.extractall(path=str(extract_dir))

            archive_path.unlink()  # free space

            # The tar has paths like: models/blobs/sha256-xxx, models/manifests/...
            # Strip the "models/" prefix — OLLAMA_MODELS will point to share root.
            models_dir = extract_dir / "models"
            if not models_dir.exists():
                raise ApplicationError(
                    f"Unexpected archive layout — no 'models/' root in tar.lz4 for {input.model_identifier}"
                )

            # ── Upload to Files share ──────────────────────────────────────────
            activity.heartbeat(f"uploading to Files share {account}/{share_name}")
            log.info("nfs_upload_started", model_identifier=input.model_identifier, account=account, share_name=share_name)

            async with files_service_client(account, account_key) as svc:
                share = svc.get_share_client(share_name)
                total_bytes = await _upload_dir_to_share(share, models_dir, remote_prefix="")

        duration = time.monotonic() - start
        await mark_files_available(input.model_identifier, input.region, total_bytes, account_key)
        log.info(
            "nfs_seed_complete",
            model_identifier=input.model_identifier,
            region=input.region,
            size_bytes=total_bytes,
            duration_s=round(duration, 1),
        )
        return SeedFilesResult(
            model_identifier=input.model_identifier,
            region=input.region,
            size_bytes=total_bytes,
            duration_seconds=duration,
        )

    except Exception:
        await mark_files_failed(input.model_identifier, input.region)
        raise


@activity.defn
async def check_files_share_ready(input: CheckFilesShareInput) -> CheckFilesShareResult:
    """Return availability of a model's NFS files share in the given region."""
    from services.files_cache import get_available_files_entry

    entry = await get_available_files_entry(input.model_identifier, input.region)
    if entry:
        return CheckFilesShareResult(
            available=True,
            storage_account=entry.storage_account,
            share_name=entry.share_name,
            account_key=entry.account_key,
        )
    return CheckFilesShareResult(available=False)
