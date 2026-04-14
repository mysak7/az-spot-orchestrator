"""Temporal workflow for server-side Azure Blob replication across regions."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporal.activities.blob import copy_blob_to_region
    from temporal.types import CopyBlobInput, CopyBlobResult

_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=10))


@workflow.defn
class CopyBlobWorkflow:
    """Replicate a cached model blob to a new Azure region using server-side copy.

    Picks the geographically nearest available source automatically — no data
    leaves Azure's internal backbone.
    """

    @workflow.run
    async def run(self, input: CopyBlobInput) -> CopyBlobResult:
        return await workflow.execute_activity(
            copy_blob_to_region,
            input,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=_RETRY,
        )
