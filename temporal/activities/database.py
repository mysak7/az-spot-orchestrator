"""Database-related Temporal activities (Azure Cosmos DB)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from azure.core.exceptions import ResourceNotFoundError
from temporalio import activity

from db.cosmos import get_instances_container, get_messages_container
from temporal.types import CreateMessageInput, UpdateVMStatusInput

log = structlog.get_logger()


@activity.defn
async def update_vm_status(input: UpdateVMStatusInput) -> None:
    """Update a VMInstance document: status, ip_address, region, and/or workflow_id.

    Uses a direct point-read (id = vm_name, partition key = vm_name) so this
    is an O(1) operation regardless of how many instances exist.
    """
    container = get_instances_container()
    try:
        item = await container.read_item(item=input.vm_name, partition_key=input.vm_name)
    except ResourceNotFoundError:
        log.warning("vm_status_update_not_found", vm_name=input.vm_name)
        return

    item["status"] = input.status
    item["updated_at"] = datetime.now(UTC).isoformat()
    if input.ip_address is not None:
        item["ip_address"] = input.ip_address
    if input.region is not None:
        item["region"] = input.region
    if input.workflow_id is not None:
        item["workflow_id"] = input.workflow_id

    await container.replace_item(item=input.vm_name, body=item)
    log.info("vm_status_updated", vm_name=input.vm_name, status=input.status)


@activity.defn
async def create_system_message(input: CreateMessageInput) -> None:
    """Persist a warning/info/error message to the system-messages Cosmos container."""
    from db.models import SystemMessage

    msg = SystemMessage(
        level=input.level,
        title=input.title,
        body=input.body,
        vm_name=input.vm_name,
    )
    container = get_messages_container()
    await container.create_item(body=msg.model_dump())
    log.info("system_message_created", level=input.level, title=input.title, vm_name=input.vm_name)
