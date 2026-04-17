"""Temporal workflow for seeding Azure Blob Storage from the Ollama registry."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporal.activities.seed_blob import seed_blob_from_registry
    from temporal.types import SeedBlobInput, SeedBlobResult

_RETRY = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=30))


@workflow.defn
class SeedBlobWorkflow:
    """Download a model from Ollama registry and upload it to Azure Blob Storage.

    No VM needed — the control plane pulls layers directly from
    registry.ollama.ai and uploads the resulting tar.gz to the target region.
    Once complete, other regions can be populated via the faster server-side
    CopyBlobWorkflow.
    """

    @workflow.run
    async def run(self, input: SeedBlobInput) -> SeedBlobResult:
        workflow.logger.info(
            "seed_blob_workflow_started: %s in %s",
            input.model_identifier,
            input.target_region,
        )
        result = await workflow.execute_activity(
            seed_blob_from_registry,
            input,
            start_to_close_timeout=timedelta(hours=3),  # large models may be slow
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=_RETRY,
        )
        workflow.logger.info(
            "seed_blob_workflow_complete: %s in %s",
            input.model_identifier,
            input.target_region,
        )
        return result
