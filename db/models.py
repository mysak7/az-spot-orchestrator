"""Domain models stored in Azure Cosmos DB.

Each class maps 1-to-1 to a Cosmos container document.
Pydantic v2 handles serialisation/deserialisation to/from the JSON dicts
that the azure-cosmos SDK returns.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class VMStatus(str, Enum):
    pending = "pending"
    provisioning = "provisioning"
    downloading = "downloading"
    running = "running"
    evicted = "evicted"
    terminated = "terminated"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LLMModel(BaseModel):
    # `id` doubles as the Cosmos document id (partition key = /id)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str | None = None
    size_mb: int
    model_identifier: str   # Ollama tag, e.g. "llama3:8b"
    vm_size: str
    created_at: str = Field(default_factory=_now)


class VMInstance(BaseModel):
    # `id` = vm_name so update_vm_status can do a direct point-read (no cross-partition needed)
    id: str          # = vm_name
    model_id: str
    model_name: str  # denormalised — avoids a join when proxying requests
    vm_name: str     # = id
    resource_group: str
    region: str | None = None
    ip_address: str | None = None
    status: VMStatus = VMStatus.pending
    workflow_id: str | None = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
