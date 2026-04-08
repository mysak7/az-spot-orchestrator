from __future__ import annotations

from pydantic import BaseModel, Field

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
        description="Azure VM SKU to use for inference",
    )


class LLMModelResponse(BaseModel):
    id: str
    name: str
    description: str | None
    size_mb: int
    model_identifier: str
    vm_size: str
    created_at: str


# ── VM Instance ────────────────────────────────────────────────────────────────

class VMInstanceResponse(BaseModel):
    id: str
    model_id: str
    model_name: str
    vm_name: str
    resource_group: str
    region: str | None
    ip_address: str | None
    status: VMStatus
    workflow_id: str | None
    created_at: str
    updated_at: str


# ── Provisioning ──────────────────────────────────────────────────────────────

class ProvisionRequest(BaseModel):
    vm_size: str | None = Field(
        None, description="Override the model's default VM size for this run"
    )


class ProvisionResponse(BaseModel):
    vm_name: str
    workflow_id: str
    status: str = "accepted"


# ── VM Lifecycle Notifications ────────────────────────────────────────────────

class VMEvictedNotification(BaseModel):
    reason: str | None = None
