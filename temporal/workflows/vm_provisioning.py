"""ProvisionVMWorkflow — end-to-end Spot VM provisioning for LLM inference.

Workflow is deterministic: all side-effects (Azure API, DB) run in Activities.
"""
from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from config import get_settings
    from cloud_init.vm_setup import generate_cloud_init
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
        ProvisionAzureVMInput,
        ProvisionVMInput,
        ProvisionVMResult,
        UpdateVMStatusInput,
        WaitForModelInput,
    )


_FAST_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))
_SLOW_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=30))


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

        # ── Step 1: cheapest region ────────────────────────────────────────
        region: str = await workflow.execute_activity(
            get_cheapest_region,
            GetCheapestRegionInput(
                vm_size=input.vm_size,
                candidate_regions=settings.azure_candidate_regions,
            ),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_FAST_RETRY,
        )

        # ── Step 2: provision VM ───────────────────────────────────────────
        cloud_init_b64 = generate_cloud_init(
            model_identifier=input.model_identifier,
            control_plane_url=settings.control_plane_url,
            vm_name=input.vm_name,
        )

        try:
            ip_address: str = await workflow.execute_activity(
                provision_azure_vm,
                ProvisionAzureVMInput(
                    vm_name=input.vm_name,
                    resource_group=input.resource_group,
                    region=region,
                    vm_size=input.vm_size,
                    model_identifier=input.model_identifier,
                    cloud_init_b64=cloud_init_b64,
                ),
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=_SLOW_RETRY,
            )
        except Exception:
            # Provision failed — clean up any partially created resources
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
