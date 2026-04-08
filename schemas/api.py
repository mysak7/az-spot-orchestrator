from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from db.models import VMStatus


# ── LLM Model ─────────────────────────────────────────────────────────────────

class LLMModelCreate(BaseModel):
    name: str = Field(
        ...,
        description="URL-safe slug used to route requests, e.g. 'llama3-8b'",
        pattern=r"^[a-z0-9][a-z0-9\-]*$",
    )
    description: str | None = None
    size_mb: int = Field(..., gt=0, description="Approximate model weights size in MB")
    model_identifier: str = Field(
        ..., description="Ollama model tag, e.g. 'llama3:8b' or 'mistral:7b'"
    )
    vm_size: str = Field(
        "Standard_NC4as_T4_v3",
        description="Azure VM SKU to use for inference (must have enough VRAM)",
    )


class LLMModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    size_mb: int
    model_identifier: str
    vm_size: str
    created_at: datetime


# ── VM Instance ────────────────────────────────────────────────────────────────

class VMInstanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    model_id: uuid.UUID
    vm_name: str
    resource_group: str
    region: str | None
    ip_address: str | None
    status: VMStatus
    workflow_id: str | None
    created_at: datetime
    updated_at: datetime


# ── Provisioning ──────────────────────────────────────────────────────────────

class ProvisionRequest(BaseModel):
    vm_size: str | None = Field(
        None, description="Override the model's default VM size for this run"
    )


class ProvisionResponse(BaseModel):
    vm_name: str
    workflow_id: str
    status: str = "accepted"


# ── VM Lifecycle Notifications (called by VM via cloud-init / eviction hook) ──

class VMEvictedNotification(BaseModel):
    reason: str | None = None
