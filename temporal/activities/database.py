"""Database-related Temporal activities.

Wraps all DB writes so the Workflow stays free of side-effects.
"""
from __future__ import annotations

import logging

from sqlalchemy import update
from temporalio import activity

from db.models import VMInstance, VMStatus
from db.session import AsyncSessionLocal
from temporal.types import UpdateVMStatusInput

logger = logging.getLogger(__name__)


@activity.defn
async def update_vm_status(input: UpdateVMStatusInput) -> None:
    """Update a VMInstance row: status, ip_address, region, and/or workflow_id."""
    values: dict = {"status": VMStatus(input.status)}
    if input.ip_address is not None:
        values["ip_address"] = input.ip_address
    if input.region is not None:
        values["region"] = input.region
    if input.workflow_id is not None:
        values["workflow_id"] = input.workflow_id

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(VMInstance).where(VMInstance.vm_name == input.vm_name).values(**values)
        )
        await session.commit()

    logger.info("VM %s → status=%s", input.vm_name, input.status)
