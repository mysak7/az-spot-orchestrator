"""Domain models stored in Azure Cosmos DB.

Each class maps 1-to-1 to a Cosmos container document.
Pydantic v2 handles serialisation/deserialisation to/from the JSON dicts
that the azure-cosmos SDK returns.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
UTC = timezone.utc  # py310 compat
try:
    from enum import StrEnum
except ImportError:  # py310 compat
    from enum import Enum
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass
from typing import Literal

from pydantic import BaseModel, Field


class VMStatus(StrEnum):
    pending = "pending"
    provisioning = "provisioning"
    downloading = "downloading"
    running = "running"
    evicted = "evicted"
    terminated = "terminated"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LLMModel(BaseModel):
    # `id` doubles as the Cosmos document id (partition key = /id)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str | None = None
    size_mb: int
    model_identifier: str  # Ollama tag, e.g. "llama3:8b"
    vm_size: str
    keep_alive: bool = False  # if True, watchdog re-provisions whenever no active VM exists
    created_at: str = Field(default_factory=_now)


class VMInstance(BaseModel):
    # `id` = vm_name so update_vm_status can do a direct point-read (no cross-partition needed)
    id: str  # = vm_name
    model_id: str | None = None
    model_name: str | None = None  # denormalised — avoids a join when proxying requests
    vm_name: str  # = id
    resource_group: str
    region: str | None = None
    ip_address: str | None = None
    status: VMStatus = VMStatus.pending
    workflow_id: str | None = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class SystemMessage(BaseModel):
    """A persistent warning/info message stored in Cosmos DB."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    level: Literal["warning", "info", "error"] = "warning"
    title: str
    body: str
    vm_name: str | None = None
    read: bool = False
    created_at: str = Field(default_factory=_now)


class FilesShareEntry(BaseModel):
    """Tracks a per-region Azure Files SMB share that has model weights pre-loaded."""

    # `id` = "{model_identifier_sanitized}-{region}" for direct point-read
    id: str
    model_identifier: str  # e.g. "smollm2:1.7b"
    region: str             # e.g. "westeurope"
    storage_account: str    # e.g. "azspotfileswesteurope"
    share_name: str         # always "models"
    account_key: str = ""   # storage account key for CIFS mount in cloud-init
    size_bytes: int = 0
    status: Literal["provisioning", "available", "failed"] = "provisioning"
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class ModelCacheEntry(BaseModel):
    # `id` = "{model_identifier_sanitized}-{region}" for direct point-read
    id: str
    model_identifier: str  # e.g. "qwen2.5:1.5b"
    region: str  # e.g. "eastus"
    blob_name: str  # e.g. "eastus/qwen2.5-1.5b.tar.gz"
    size_bytes: int = 0
    status: Literal["uploading", "available", "failed"] = "uploading"
    current_phase: str | None = None  # "pulling" | "archiving" | "uploading" | None
    download_progress_pct: int | None = None  # 0-100 during pulling phase
    upload_started_at: str
    upload_completed_at: str | None = None
    upload_duration_seconds: float | None = None
    last_download_duration_seconds: float | None = None
    download_count: int = 0
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
