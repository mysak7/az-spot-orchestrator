"""Temporal activities for Azure Blob Storage copy operations."""

from __future__ import annotations

import asyncio
import logging
import time

from azure.core.exceptions import AzureError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from config import get_settings
from services.blob_client import blob_service_client
from services.model_cache import (
    _blob_name,
    find_best_copy_source,
    get_blob_read_url,
    mark_copy_started,
    register_upload_complete,
)
from temporal.types import CopyBlobInput, CopyBlobResult

logger = logging.getLogger(__name__)


@activity.defn
async def copy_blob_to_region(input: CopyBlobInput) -> CopyBlobResult:
    """Server-side Azure Blob copy from the nearest available region to target_region.

    Finds the geographically nearest region that already has the model cached,
    then uses Azure's async server-side copy — no data leaves Azure's backbone.
    """
    s = get_settings()

    source_region = await find_best_copy_source(input.model_identifier, input.target_region)
    if source_region is None:
        raise ApplicationError(
            f"No available blob source for {input.model_identifier} — provision a VM first",
            non_retryable=True,
        )

    logger.info(
        "Starting blob copy %s: %s → %s",
        input.model_identifier,
        source_region,
        input.target_region,
    )

    await mark_copy_started(input.model_identifier, input.target_region)
    source_url = await get_blob_read_url(input.model_identifier, source_region)
    target_blob_name = _blob_name(input.model_identifier, input.target_region)

    start = time.monotonic()

    async with blob_service_client() as client:
        target_client = client.get_blob_client(
            container=s.azure_storage_container_name,
            blob=target_blob_name,
        )
        try:
            await target_client.start_copy_from_url(source_url)
        except AzureError as exc:
            raise ApplicationError(
                f"Failed to start blob copy {input.model_identifier} → {input.target_region}: {exc}",
                non_retryable=True,
            ) from exc

        while True:
            activity.heartbeat(f"copying {input.model_identifier} → {input.target_region}")
            props = await target_client.get_blob_properties()
            copy_props = props.copy  # type: ignore[union-attr]
            copy_status = copy_props.status if copy_props is not None else None
            if copy_status == "success":
                size_bytes: int = props.size  # type: ignore[assignment]
                break
            if copy_status in ("failed", "aborted"):
                desc = getattr(copy_props, "status_description", copy_status)
                raise ApplicationError(
                    f"Blob copy {copy_status}: {desc}", non_retryable=True
                )
            if copy_status is None:
                raise ApplicationError(
                    f"Blob copy metadata unavailable for {target_blob_name} — copy may not have started",
                    non_retryable=True,
                )
            await asyncio.sleep(5)

    duration = time.monotonic() - start
    await register_upload_complete(
        input.model_identifier, input.target_region, size_bytes, duration
    )
    logger.info(
        "Blob copy complete: %s → %s in %.1fs (%d bytes)",
        source_region,
        input.target_region,
        duration,
        size_bytes,
    )
    return CopyBlobResult(
        model_identifier=input.model_identifier,
        source_region=source_region,
        target_region=input.target_region,
        size_bytes=size_bytes,
        duration_seconds=duration,
    )
