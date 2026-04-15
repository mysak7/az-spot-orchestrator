"""Temporal activities for Azure Files NFS share infrastructure and model seeding.

Two activities:
  1. ensure_files_infrastructure  — creates FileStorage account + NFS share + network rules
  2. seed_files_from_blob         — extracts tar.lz4 blob and uploads model files to share
"""

from __future__ import annotations

import io
import logging
import lz4.frame
import os
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
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
from services.model_cache import _blob_name, get_blob_read_url
from temporal.types import (
    CheckFilesShareInput,
    CheckFilesShareResult,
    EnsureFilesInfraInput,
    EnsureFilesInfraResult,
    SeedFilesInput,
    SeedFilesResult,
)

logger = logging.getLogger(__name__)


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
                logger.info("Adding Microsoft.Storage service endpoint to subnet %s", subnet_name)
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
    logger.info("Subnet with storage endpoint: %s", subnet_id)

    activity.heartbeat(f"creating FileStorage account {account}")
    async with storage_mgmt_client() as mgmt:
        # Create or update storage account
        try:
            existing = await mgmt.storage_accounts.get_properties(input.resource_group, account)
            logger.info("FileStorage account %s already exists", account)
        except ResourceNotFoundError:
            existing = None

        if existing is None:
            params = StorageAccountCreateParameters(
                sku=Sku(name=SkuName.PREMIUM_LRS),
                kind=Kind.FILE_STORAGE,
                location=input.region,
                enable_https_traffic_only=False,  # NFS 4.1 requires HTTP
                network_rule_set=NetworkRuleSet(
                    default_action=DefaultAction.ALLOW,  # start open; restrict after share is ready
                    bypass=["AzureServices"],
                ),
            )
            poller = await mgmt.storage_accounts.begin_create(input.resource_group, account, params)
            await poller.result()
            logger.info("Created FileStorage account %s in %s", account, input.region)
        else:
            # Ensure HTTPS-only is disabled (needed for NFS)
            if existing.enable_https_traffic_only:
                from azure.mgmt.storage.models import StorageAccountUpdateParameters
                update_poller = await mgmt.storage_accounts.begin_update(
                    input.resource_group, account,
                    StorageAccountUpdateParameters(enable_https_traffic_only=False),
                )
                await update_poller.result()
                logger.info("Disabled https-only on %s", account)

        # Update network rules: allow subnet (VNet rule) + control plane IP
        # We do this as a separate update after creation to avoid race conditions
        activity.heartbeat(f"configuring network rules on {account}")
        try:
            from azure.mgmt.storage.models import StorageAccountUpdateParameters
            update_poller = await mgmt.storage_accounts.begin_update(
                input.resource_group,
                account,
                StorageAccountUpdateParameters(
                    network_rule_set=NetworkRuleSet(
                        default_action=DefaultAction.ALLOW,
                        bypass=["AzureServices"],
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
            await update_poller.result()
            logger.info("Network rules set on %s", account)
        except Exception as exc:
            logger.warning("Network rules update failed (non-fatal): %s", exc)

        # Get storage account key for share operations
        activity.heartbeat(f"getting storage account key for {account}")
        keys_result = await mgmt.storage_accounts.list_keys(input.resource_group, account)
        account_key = (keys_result.keys or [])[0].value
        if not account_key:
            raise ApplicationError(f"Could not retrieve key for storage account {account}")

        # Create NFS share
        activity.heartbeat(f"creating NFS share '{share_name}' on {account}")
        try:
            await mgmt.file_shares.create(
                input.resource_group,
                account,
                share_name,
                FileShare(
                    enabled_protocols="NFS",
                    root_squash="NoRootSquash",
                    share_quota=5120,  # 5 TB max, billed per-GB used
                ),
            )
            logger.info("Created NFS share '%s' on %s", share_name, account)
        except ResourceExistsError:
            logger.info("NFS share '%s' already exists on %s", share_name, account)
        except Exception as exc:
            # If NFS protocol fails (e.g. not yet propagated), fall back to SMB share
            logger.warning("NFS share creation failed (%s), trying without protocol spec", exc)
            try:
                await mgmt.file_shares.create(
                    input.resource_group, account, share_name,
                    FileShare(share_quota=5120),
                )
                logger.info("Created standard share '%s' on %s", share_name, account)
            except ResourceExistsError:
                pass

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
                await file_client.upload_file(fh, length=file_size, overwrite=True)

            total_bytes += file_size
            logger.debug("Uploaded %s (%d bytes)", remote_file_path, file_size)

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

    await mark_files_provisioning(input.model_identifier, input.region)
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

        # ── Download + extract to temp dir ─────────────────────────────────────
        with tempfile.TemporaryDirectory(prefix="ollama_files_") as tmpdir:
            tmp_path = Path(tmpdir)
            archive_path = tmp_path / "model.tar.lz4"

            activity.heartbeat("downloading blob archive")
            logger.info("Downloading blob for %s in %s", input.model_identifier, input.region)

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
            logger.info("Downloaded %d bytes, extracting...", archive_size)

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
            logger.info("Uploading model files to %s/%s", account, share_name)

            async with files_service_client(account, account_key) as svc:
                share = svc.get_share_client(share_name)
                total_bytes = await _upload_dir_to_share(share, models_dir, remote_prefix="")

        duration = time.monotonic() - start
        await mark_files_available(input.model_identifier, input.region, total_bytes)
        logger.info(
            "Files seeding complete: %s in %s — %d bytes in %.1fs",
            input.model_identifier, input.region, total_bytes, duration,
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
        )
    return CheckFilesShareResult(available=False)
