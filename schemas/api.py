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
    keep_alive: bool = False
    created_at: str


class LLMModelPatch(BaseModel):
    description: str | None = None
    size_mb: int | None = Field(None, gt=0)
    model_identifier: str | None = None
    vm_size: str | None = None
    keep_alive: bool | None = None


# ── VM Instance ────────────────────────────────────────────────────────────────


class VMInstanceResponse(BaseModel):
    id: str
    model_id: str | None
    model_name: str | None
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


# ── Model Cache ────────────────────────────────────────────────────────────────────


class CacheSourceResponse(BaseModel):
    """Response for GET /api/storage/cache/source — tells VM where to get the model."""

    source: str  # "blob" | "ollama"
    download_url: str | None = None  # SAS URL to download from blob (if blob found)


class CacheCompleteRequest(BaseModel):
    """Request body for POST /api/storage/cache/complete — Seed workflow registers upload."""

    model_identifier: str
    region: str
    size_bytes: int
    duration_seconds: float


class ModelCacheEntryResponse(BaseModel):
    """Response for GET /api/storage/cache — list of cached models."""

    id: str
    model_identifier: str
    region: str
    blob_name: str
    size_bytes: int
    status: str
    current_phase: str | None  # "pulling" | "archiving" | "uploading" | None (when done/failed)
    download_progress_pct: int | None  # 0-100 during pulling phase, None otherwise
    upload_started_at: str
    upload_completed_at: str | None
    upload_duration_seconds: float | None
    last_download_duration_seconds: float | None
    download_count: int
    created_at: str
    updated_at: str


class CacheProgressRequest(BaseModel):
    """Request body for POST /api/storage/cache/progress — VM reports current phase."""

    model_identifier: str
    region: str
    phase: str  # "pulling" | "archiving" | "uploading"


class CacheFailedRequest(BaseModel):
    """Request body for POST /api/storage/cache/failed — VM reports upload failure."""

    model_identifier: str
    region: str


class CopyBlobRequest(BaseModel):
    """Request body for POST /api/storage/cache/copy — trigger server-side blob replication."""

    model_identifier: str
    target_region: str


class CreateFilesShareRequest(BaseModel):
    """Request body for POST /api/storage/files/create-share — provision NFS share infrastructure."""

    region: str
