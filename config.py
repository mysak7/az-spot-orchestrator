from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass
class DefaultModel:
    """A model that should be seeded into the registry on startup if absent."""

    name: str
    model_identifier: str
    size_mb: int
    vm_size: str
    description: str | None = None


# Models automatically registered on first startup.
# Add new entries here to ensure they survive redeployments.
DEFAULT_MODELS: list[DefaultModel] = [
    DefaultModel(
        name="qwen25-1b5",
        model_identifier="qwen2.5:1.5b",
        size_mb=934,
        vm_size="Standard_NC4as_T4_v3",
        description="Qwen 2.5 1.5B – lightweight general-purpose model",
    ),
    DefaultModel(
        name="qwen25-3b-instruct",
        model_identifier="qwen2.5:3b",
        size_mb=1900,
        vm_size="Standard_D2s_v3",
        description="Qwen 2.5 3B Instruct – CPU inference, 8 GB RAM",
    ),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Azure Cosmos DB (app state store)
    cosmos_endpoint: str = ""
    cosmos_key: str = ""

    # Azure service principal
    azure_subscription_id: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_resource_group: str = "az-spot-orchestrator-rg"
    azure_ssh_public_key: str = ""

    # Temporal (dev server uses SQLite — no external DB needed)
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "vm-provisioning"

    # Control plane URL (embedded into cloud-init for VM callbacks)
    control_plane_url: str = "http://localhost:8000"

    # VM provisioning defaults
    default_vm_size: str = "Standard_NC4as_T4_v3"
    azure_candidate_regions: list[str] = [
        "eastus",
        "westus2",
        "eastus2",
        "westeurope",
        "northeurope",
        "southeastasia",
        "australiaeast",
        "japaneast",
    ]

    # Azure Blob Storage (model cache)
    azure_storage_account_name: str = ""
    azure_storage_container_name: str = "model-cache"


@lru_cache
def get_settings() -> Settings:
    return Settings()
