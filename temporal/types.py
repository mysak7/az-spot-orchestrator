"""Shared dataclasses for Temporal workflow/activity inputs and outputs.

Using stdlib dataclasses (not Pydantic) for Temporal serialisation compatibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProvisionVMInput:
    model_id: str
    model_name: str
    model_identifier: str   # e.g. "llama3:8b"
    vm_size: str
    vm_name: str
    resource_group: str


@dataclass
class ProvisionVMResult:
    vm_name: str
    region: str
    ip_address: str


@dataclass
class GetCheapestRegionInput:
    vm_size: str
    candidate_regions: list[str] = field(default_factory=list)


@dataclass
class ProvisionAzureVMInput:
    vm_name: str
    resource_group: str
    region: str
    vm_size: str
    model_identifier: str
    cloud_init_b64: str     # base64-encoded cloud-config YAML


@dataclass
class WaitForModelInput:
    ip_address: str
    model_identifier: str
    timeout_minutes: int = 45


@dataclass
class UpdateVMStatusInput:
    vm_name: str
    status: str
    ip_address: str | None = None
    region: str | None = None
    workflow_id: str | None = None


@dataclass
class DeleteAzureVMInput:
    vm_name: str
    resource_group: str
