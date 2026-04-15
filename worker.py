"""Temporal worker — registers all workflows and activities, then runs indefinitely."""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from config import get_settings
from temporal.activities.azure import (
    delete_azure_vm,
    get_cheapest_region,
    provision_azure_vm,
    wait_for_model_ready,
)
from temporal.activities.blob import copy_blob_to_region
from temporal.activities.database import create_system_message, update_vm_status
from temporal.activities.files import (
    check_files_share_ready,
    ensure_files_infrastructure,
    seed_files_from_blob,
)
from temporal.activities.seed_blob import seed_blob_from_registry
from temporal.workflows.blob_copy import CopyBlobWorkflow
from temporal.workflows.seed_blob import SeedBlobWorkflow
from temporal.workflows.seed_files import SeedFilesWorkflow
from temporal.workflows.vm_provisioning import (
    DeleteVMWorkflow,
    LaunchBareVMWorkflow,
    ProvisionVMWorkflow,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            ProvisionVMWorkflow,
            DeleteVMWorkflow,
            LaunchBareVMWorkflow,
            CopyBlobWorkflow,
            SeedBlobWorkflow,
            SeedFilesWorkflow,
        ],
        activities=[
            get_cheapest_region,
            provision_azure_vm,
            wait_for_model_ready,
            delete_azure_vm,
            update_vm_status,
            create_system_message,
            copy_blob_to_region,
            seed_blob_from_registry,
            ensure_files_infrastructure,
            seed_files_from_blob,
            check_files_share_ready,
        ],
    )

    logger.info("Worker listening on task queue '%s'", settings.temporal_task_queue)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
