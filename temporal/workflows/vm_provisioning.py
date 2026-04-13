"""ProvisionVMWorkflow — end-to-end Spot VM provisioning for LLM inference.

Workflow is deterministic: all side-effects (Azure API, DB) run in Activities.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from cloud_init.vm_setup import generate_bare_cloud_init, generate_cloud_init
    from config import get_settings
    from temporal.activities.azure import (
        delete_azure_vm,
        get_cheapest_region,
        provision_azure_vm,
        wait_for_model_ready,
    )
    from temporal.activities.database import update_vm_status
    from temporal.types import (
        DeleteAzureVMInput,
        GetCheapestRegionInput,
        LaunchBareVMInput,
        LaunchBareVMResult,
        ProvisionAzureVMInput,
        ProvisionVMInput,
        ProvisionVMResult,
        UpdateVMStatusInput,
        WaitForModelInput,
    )


_FAST_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))
_SLOW_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=30))


@workflow.defn
class DeleteVMWorkflow:
    """Delete an evicted Spot VM and its Azure resources (NIC, public IP).

    Used after eviction to release Spot vCPU quota so the next provision
    attempt succeeds.
    """

    @workflow.run
    async def run(self, input: DeleteAzureVMInput) -> None:
        await workflow.execute_activity(
            delete_azure_vm,
            input,
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=_FAST_RETRY,
        )


@workflow.defn
class ProvisionVMWorkflow:
    """Orchestrate provisioning a Spot VM and waiting for the model to be ready.

    Steps:
      1. Find cheapest Azure region for the requested VM size.
      2. Provision a Spot VM with Ollama cloud-init in that region.
      3. Update DB to 'provisioning' with region + IP.
      4. Poll Ollama until the model is downloaded ('downloading' → 'running').
      5. Mark VM as 'running' in DB.

    On failure the VM is cleaned up via a compensation activity.
    """

    @workflow.run
    async def run(self, input: ProvisionVMInput) -> ProvisionVMResult:
        settings = get_settings()

        # ── Step 1: regions ordered cheapest-first ────────────────────────
        regions: list[str] = await workflow.execute_activity(
            get_cheapest_region,
            GetCheapestRegionInput(
                vm_size=input.vm_size,
                candidate_regions=settings.azure_candidate_regions,
            ),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_FAST_RETRY,
        )

        # ── Step 2: provision VM, falling back through regions on SkuNotAvailable ──
        # Note: get_cheapest_region raises InsufficientSpotQuota (non_retryable) if
        # the subscription's lowPriorityCores limit is too low for input.vm_size —
        # that error propagates out of the loop below without trying any region.
        cloud_init_b64 = generate_cloud_init(
            model_identifier=input.model_identifier,
            control_plane_url=settings.control_plane_url,
            vm_name=input.vm_name,
        )

        ip_address: str = ""
        region: str = ""
        last_error: BaseException | None = None

        for candidate in regions:
            try:
                ip_address = await workflow.execute_activity(
                    provision_azure_vm,
                    ProvisionAzureVMInput(
                        vm_name=input.vm_name,
                        resource_group=input.resource_group,
                        region=candidate,
                        vm_size=input.vm_size,
                        model_identifier=input.model_identifier,
                        cloud_init_b64=cloud_init_b64,
                    ),
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                region = candidate
                break
            except ActivityError as exc:
                if (
                    isinstance(exc.__cause__, ApplicationError)
                    and exc.__cause__.type == "SkuNotAvailable"
                ):
                    workflow.logger.warning(
                        "No Spot capacity for %s in %s, trying next region",
                        input.vm_size,
                        candidate,
                    )
                    # Clean up partial resources before trying next region
                    await workflow.execute_activity(
                        delete_azure_vm,
                        DeleteAzureVMInput(
                            vm_name=input.vm_name,
                            resource_group=input.resource_group,
                        ),
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=_FAST_RETRY,
                    )
                    last_error = exc
                    continue
                # Non-capacity failure: clean up and abort
                await workflow.execute_activity(
                    delete_azure_vm,
                    DeleteAzureVMInput(
                        vm_name=input.vm_name,
                        resource_group=input.resource_group,
                    ),
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_FAST_RETRY,
                )
                await workflow.execute_activity(
                    update_vm_status,
                    UpdateVMStatusInput(vm_name=input.vm_name, status="terminated"),
                    start_to_close_timeout=timedelta(minutes=2),
                )
                raise

        if not ip_address:
            await workflow.execute_activity(
                update_vm_status,
                UpdateVMStatusInput(vm_name=input.vm_name, status="terminated"),
                start_to_close_timeout=timedelta(minutes=2),
            )
            raise ApplicationError(
                f"No region had Spot capacity for {input.vm_size}. Tried: {regions}",
                non_retryable=True,
            ) from last_error

        # ── Step 3: mark provisioning ──────────────────────────────────────
        await workflow.execute_activity(
            update_vm_status,
            UpdateVMStatusInput(
                vm_name=input.vm_name,
                status="provisioning",
                region=region,
                ip_address=ip_address,
                workflow_id=workflow.info().workflow_id,
            ),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_FAST_RETRY,
        )

        # ── Step 4: wait for model download ───────────────────────────────
        await workflow.execute_activity(
            update_vm_status,
            UpdateVMStatusInput(vm_name=input.vm_name, status="downloading"),
            start_to_close_timeout=timedelta(minutes=2),
        )

        await workflow.execute_activity(
            wait_for_model_ready,
            WaitForModelInput(
                ip_address=ip_address,
                model_identifier=input.model_identifier,
                timeout_minutes=45,
            ),
            start_to_close_timeout=timedelta(minutes=50),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=_SLOW_RETRY,
        )

        # ── Step 5: mark running ──────────────────────────────────────────
        await workflow.execute_activity(
            update_vm_status,
            UpdateVMStatusInput(vm_name=input.vm_name, status="running"),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_FAST_RETRY,
        )

        return ProvisionVMResult(
            vm_name=input.vm_name,
            region=region,
            ip_address=ip_address,
        )


@workflow.defn
class LaunchBareVMWorkflow:
    """Provision a Spot VM with SSH access only — no Ollama/model.

    Useful for ad-hoc GPU exploration, debugging, or manual workloads.
    Steps:
      1. Resolve candidate regions (cheapest first, or use the specified region).
      2. Provision Spot VM with bare cloud-init (SSH + basic tools).
      3. Return IP address.
    """

    @workflow.run
    async def run(self, input: LaunchBareVMInput) -> LaunchBareVMResult:
        settings = get_settings()

        cloud_init_b64 = generate_bare_cloud_init(
            vm_name=input.vm_name,
            control_plane_url=settings.control_plane_url,
        )

        # If caller specified a region use only that, otherwise rank all candidates.
        if input.region:
            regions = [input.region]
        else:
            regions = await workflow.execute_activity(
                get_cheapest_region,
                GetCheapestRegionInput(
                    vm_size=input.vm_size,
                    candidate_regions=settings.azure_candidate_regions,
                ),
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_FAST_RETRY,
            )

        ip_address = ""
        region = ""
        last_error: BaseException | None = None

        for candidate in regions:
            try:
                ip_address = await workflow.execute_activity(
                    provision_azure_vm,
                    ProvisionAzureVMInput(
                        vm_name=input.vm_name,
                        resource_group=input.resource_group,
                        region=candidate,
                        vm_size=input.vm_size,
                        model_identifier="",
                        cloud_init_b64=cloud_init_b64,
                    ),
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                region = candidate
                break
            except ActivityError as exc:
                cause = exc.__cause__
                if isinstance(cause, ApplicationError) and cause.type == "SkuNotAvailable":
                    workflow.logger.warning(
                        "No Spot capacity for %s in %s, trying next region",
                        input.vm_size,
                        candidate,
                    )
                    await workflow.execute_activity(
                        delete_azure_vm,
                        DeleteAzureVMInput(
                            vm_name=input.vm_name,
                            resource_group=input.resource_group,
                        ),
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=_FAST_RETRY,
                    )
                    last_error = exc
                    continue
                # Non-capacity failure — clean up then surface as ApplicationError
                # so Temporal marks the workflow FAILED (not retries as task failure).
                await workflow.execute_activity(
                    delete_azure_vm,
                    DeleteAzureVMInput(
                        vm_name=input.vm_name,
                        resource_group=input.resource_group,
                    ),
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_FAST_RETRY,
                )
                raise ApplicationError(
                    str(cause.message if isinstance(cause, ApplicationError) else exc),
                    non_retryable=True,
                ) from exc

        if not ip_address:
            raise ApplicationError(
                f"No region had Spot capacity for {input.vm_size}. Tried: {regions}",
                non_retryable=True,
            ) from last_error

        return LaunchBareVMResult(
            vm_name=input.vm_name,
            region=region,
            ip_address=ip_address,
        )
