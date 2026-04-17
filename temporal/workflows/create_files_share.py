"""Temporal workflow: provision Azure Files NFS share infrastructure for a region.

Unlike SeedFilesWorkflow, this workflow only creates the storage account + NFS
share and does NOT copy model weights.  Use it to pre-provision the share so
that a later SeedFilesWorkflow (or manual copy) can populate it.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporal.activities.files import ensure_files_infrastructure
    from temporal.types import EnsureFilesInfraInput, EnsureFilesInfraResult

_INFRA_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=30))


@workflow.defn
class CreateFilesShareWorkflow:
    """Create the Azure Files NFS share infrastructure for a region.

    Runs only ``ensure_files_infrastructure`` — no model data is copied.
    Idempotent: safe to run even if the share already exists.
    """

    @workflow.run
    async def run(self, input: EnsureFilesInfraInput) -> EnsureFilesInfraResult:
        workflow.logger.info("create_files_share_workflow_started: region=%s", input.region)
        result = await workflow.execute_activity(
            ensure_files_infrastructure,
            input,
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=_INFRA_RETRY,
        )
        workflow.logger.info("create_files_share_workflow_complete: region=%s", input.region)
        return result
