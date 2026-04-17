#!/usr/bin/env python3
"""End-to-end test for the Azure Files NFS pipeline.

Tests:
  1. Blob cache exists for test model (or seeds it)
  2. SeedFilesWorkflow: creates FileStorage account + NFS share + uploads model files
  3. ProvisionVMWorkflow: provisions VM using NFS mount (fast path)
  4. VM reaches 'running' status within 10 min
  5. Inference via Ollama API succeeds

Run from repo root:
  python scripts/test_files_pipeline.py

Set TEST_REGION env var to override (default: swedencentral).
Set TEST_MODEL env var to override (default: smollm2:1.7b).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from temporalio.client import Client

from config import get_settings
from db.cosmos import get_cache_container, setup_cosmos
from services.files_cache import files_account_name, get_available_files_entry, get_files_entry
from services.model_cache import _blob_name
from temporal.types import SeedBlobInput, SeedFilesInput
from temporal.workflows.seed_blob import SeedBlobWorkflow
from temporal.workflows.seed_files import SeedFilesWorkflow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
# Suppress noisy Azure HTTP wire logging
for _noisy in ("azure.core.pipeline.policies.http_logging_policy",
               "azure.cosmos._cosmos_http_logging_policy",
               "azure.identity.aio._credentials.environment",
               "azure.identity.aio._credentials.managed_identity",
               "azure.identity.aio._credentials.chained"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("test_files_pipeline")

TEST_REGION = os.environ.get("TEST_REGION", "swedencentral")
TEST_MODEL = os.environ.get("TEST_MODEL", "smollm2:1.7b")
TIMEOUT_SEED_BLOB = 30 * 60    # 30 min
TIMEOUT_SEED_FILES = 20 * 60   # 20 min
TIMEOUT_VM_READY = 10 * 60     # 10 min


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    sys.exit(1)


def _info(msg: str) -> None:
    print(f"  [..] {msg}")


async def _wait_workflow(client: Client, wf_handle: object, label: str, timeout: int) -> object:
    """Poll workflow until terminal state, raise on failure."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            result = await asyncio.wait_for(
                wf_handle.result(),  # type: ignore[union-attr]
                timeout=5,
            )
            return result
        except asyncio.TimeoutError:
            elapsed = int(time.monotonic() - start)
            _info(f"{label}: waiting... ({elapsed}s)")
        except Exception as exc:
            _fail(f"{label} failed: {exc}")
    _fail(f"{label} timed out after {timeout}s")


async def step_ensure_blob(client: Client, settings: object) -> None:
    """Ensure blob cache exists for test model in test region."""
    print("\n[1/4] Checking blob cache...")
    container = get_cache_container()
    blob = _blob_name(TEST_MODEL, TEST_REGION)
    entry_id = f"{TEST_MODEL.replace(':', '-').replace('.', '-')}-{TEST_REGION}"

    try:
        item = await container.read_item(entry_id, partition_key=entry_id)
        if item.get("status") == "available":
            _ok(f"Blob already cached: {blob}")
            return
    except Exception:
        pass

    _info(f"Seeding blob for {TEST_MODEL} in {TEST_REGION}...")
    safe_id = TEST_MODEL.replace(":", "-").replace(".", "-")
    wf_id = f"test-seed-blob-{safe_id}-{TEST_REGION}-{uuid.uuid4().hex[:6]}"
    handle = await client.start_workflow(
        SeedBlobWorkflow.run,
        SeedBlobInput(model_identifier=TEST_MODEL, target_region=TEST_REGION),
        id=wf_id,
        task_queue=settings.temporal_task_queue,  # type: ignore[union-attr]
    )
    await _wait_workflow(client, handle, "SeedBlobWorkflow", TIMEOUT_SEED_BLOB)
    _ok(f"Blob seeded: {blob}")


async def step_seed_files(client: Client, settings: object) -> None:
    """Seed Azure Files NFS share with model weights."""
    print(f"\n[2/4] Seeding Azure Files NFS share (account: {files_account_name(TEST_REGION)})...")

    # Check if already done
    entry = await get_available_files_entry(TEST_MODEL, TEST_REGION)
    if entry:
        _ok(f"Files share already available: {entry.storage_account}/{entry.share_name}")
        return

    # Check if in-progress
    in_progress = await get_files_entry(TEST_MODEL, TEST_REGION)
    if in_progress and in_progress.status == "provisioning":
        _info("Files share seeding already in progress — will wait for it")

    safe_id = TEST_MODEL.replace(":", "-").replace(".", "-")
    wf_id = f"test-seed-files-{safe_id}-{TEST_REGION}-{uuid.uuid4().hex[:6]}"
    handle = await client.start_workflow(
        SeedFilesWorkflow.run,
        SeedFilesInput(
            model_identifier=TEST_MODEL,
            region=TEST_REGION,
            resource_group=settings.azure_resource_group,  # type: ignore[union-attr]
        ),
        id=wf_id,
        task_queue=settings.temporal_task_queue,  # type: ignore[union-attr]
    )
    result = await _wait_workflow(client, handle, "SeedFilesWorkflow", TIMEOUT_SEED_FILES)
    _ok(f"Files seeded: {result.size_bytes // 1024 // 1024} MB in {result.duration_seconds:.0f}s")


async def step_provision_vm(client: Client, settings: object) -> tuple[str, str]:
    """Provision a VM using the NFS path and return (vm_name, ip_address)."""
    print(f"\n[3/4] Provisioning VM with NFS mount in {TEST_REGION}...")

    from db.cosmos import get_instances_container
    from db.models import VMInstance, VMStatus
    from temporal.types import ProvisionVMInput
    from temporal.workflows.vm_provisioning import ProvisionVMWorkflow

    # Find model record
    from db.cosmos import get_models_container
    models_container = get_models_container()
    model_item = None
    async for item in models_container.query_items(
        query="SELECT * FROM c WHERE c.model_identifier = @mid",
        parameters=[{"name": "@mid", "value": TEST_MODEL}],
    ):
        model_item = item
        break

    if not model_item:
        _fail(f"Model {TEST_MODEL} not found in registry — add it first via the API")

    vm_name = f"test-nfs-{uuid.uuid4().hex[:8]}"
    wf_id = f"provision-{vm_name}"

    instances_container = get_instances_container()
    instance = VMInstance(
        id=vm_name,
        model_id=model_item["id"],
        model_name=model_item["name"],
        vm_name=vm_name,
        resource_group=settings.azure_resource_group,  # type: ignore[union-attr]
        status=VMStatus.pending,
        workflow_id=wf_id,
    )
    await instances_container.create_item(body=instance.model_dump())

    start = time.monotonic()
    handle = await client.start_workflow(
        ProvisionVMWorkflow.run,
        ProvisionVMInput(
            model_id=model_item["id"],
            model_name=model_item["name"],
            model_identifier=TEST_MODEL,
            vm_size=model_item["vm_size"],
            vm_name=vm_name,
            resource_group=settings.azure_resource_group,  # type: ignore[union-attr]
            force_regions=[TEST_REGION],
        ),
        id=wf_id,
        task_queue=settings.temporal_task_queue,  # type: ignore[union-attr]
    )

    result = await _wait_workflow(client, handle, "ProvisionVMWorkflow", TIMEOUT_VM_READY)
    elapsed = time.monotonic() - start
    _ok(f"VM ready: {result.vm_name} @ {result.ip_address} in {elapsed:.0f}s")
    return result.vm_name, result.ip_address


async def step_test_inference(ip_address: str) -> None:
    """Verify Ollama API responds and model is listed."""
    print(f"\n[4/4] Testing inference on {ip_address}:11434...")

    async with httpx.AsyncClient(timeout=30.0) as http:
        # Check /api/tags
        try:
            resp = await http.get(f"http://{ip_address}:11434/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            base = TEST_MODEL.split(":")[0]
            found = [m for m in models if base in m]
            if found:
                _ok(f"Model listed in /api/tags: {found[0]}")
            else:
                _fail(f"Model {TEST_MODEL} not in /api/tags. Got: {models}")
        except Exception as exc:
            _fail(f"Ollama API unreachable: {exc}")

        # Quick generate test
        _info("Sending test prompt...")
        try:
            resp = await http.post(
                f"http://{ip_address}:11434/api/generate",
                json={"model": TEST_MODEL, "prompt": "Say hello in one word.", "stream": False},
                timeout=60.0,
            )
            resp.raise_for_status()
            response_text = resp.json().get("response", "")
            _ok(f"Inference succeeded: '{response_text[:80]}'")
        except Exception as exc:
            _fail(f"Inference failed: {exc}")


async def main() -> None:
    print("\n=== Azure Files NFS Pipeline Test ===")
    print(f"  Model:  {TEST_MODEL}")
    print(f"  Region: {TEST_REGION}")
    print(f"  Target: FileStorage account '{files_account_name(TEST_REGION)}'")
    print()

    settings = get_settings()
    await setup_cosmos()

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)

    try:
        await step_ensure_blob(client, settings)
        await step_seed_files(client, settings)
        vm_name, ip = await step_provision_vm(client, settings)
        await step_test_inference(ip)

        print("\n=== ALL STEPS PASSED ===")
        print(f"  VM {vm_name} @ {ip} is running {TEST_MODEL} via NFS mount")
        print(f"  Boot path: NFS (Azure Files {files_account_name(TEST_REGION)})")

    except SystemExit:
        raise
    except Exception as exc:
        logger.exception("Unexpected error")
        _fail(str(exc))


if __name__ == "__main__":
    asyncio.run(main())
