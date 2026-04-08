from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/az_spot_orchestrator"

    # Azure service principal
    azure_subscription_id: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_resource_group: str = "az-spot-orchestrator-rg"
    azure_ssh_public_key: str = ""

    # Temporal
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
