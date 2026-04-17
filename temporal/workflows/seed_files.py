"""Temporal workflow: seed model weights to Azure Files NFS share in a region."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporal.activities.files import ensure_files_infrastructure, seed_files_from_blob
    from temporal.types import (
        EnsureFilesInfraInput,
        SeedFilesInput,
        SeedFilesResult,
    )

_INFRA_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=30))
_SEED_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(minutes=1))


@workflow.defn
class SeedFilesWorkflow:
    """Ensure Azure Files NFS share exists for a region and seed it with model weights.

    Steps:
      1. ensure_files_infrastructure — create FileStorage account + NFS share + VNet rules.
      2. seed_files_from_blob        — download tar.lz4 blob, extract, upload to share.

    Once complete, ProvisionVMWorkflow will prefer the NFS path for this model/region,
    reducing VM boot time from 15-45 min (blob download) to ~2 min (mount only).
    """

    @workflow.run
    async def run(self, input: SeedFilesInput) -> SeedFilesResult:
        workflow.logger.info(
            "seed_files_workflow_started: %s in %s",
            input.model_identifier,
            input.region,
        )

        await workflow.execute_activity(
            ensure_files_infrastructure,
            EnsureFilesInfraInput(
                region=input.region,
                resource_group=input.resource_group,
            ),
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=_INFRA_RETRY,
        )
        workflow.logger.info(
            "seed_files_infra_ready: %s in %s",
            input.model_identifier,
            input.region,
        )

        result = await workflow.execute_activity(
            seed_files_from_blob,
            input,
            start_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=_SEED_RETRY,
        )
        workflow.logger.info(
            "seed_files_workflow_complete: %s in %s",
            input.model_identifier,
            input.region,
        )
        return result
