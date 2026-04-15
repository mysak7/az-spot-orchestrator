"""Shared dataclasses for Temporal workflow/activity inputs and outputs.

Using stdlib dataclasses (not Pydantic) for Temporal serialisation compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProvisionVMInput:
    model_id: str
    model_name: str
    model_identifier: str  # e.g. "llama3:8b"
    vm_size: str
    vm_name: str
    resource_group: str
    force_regions: list[str] | None = None  # when set, try only these regions in order


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
class GetCheapestRegionResult:
    regions: list[str]        # ordered cheapest-first
    prices: dict[str, float]  # region → $/hr spot price (empty if pricing API failed)


@dataclass
class CreateMessageInput:
    level: str   # "warning" | "info" | "error"
    title: str
    body: str
    vm_name: str | None = None


@dataclass
class ProvisionAzureVMInput:
    vm_name: str
    resource_group: str
    region: str
    vm_size: str
    model_identifier: str
    cloud_init_b64: str  # base64-encoded cloud-config YAML


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


@dataclass
class LaunchBareVMInput:
    vm_name: str
    resource_group: str
    vm_size: str
    region: str | None = None  # None = auto-select cheapest


@dataclass
class LaunchBareVMResult:
    vm_name: str
    region: str
    ip_address: str


@dataclass
class CopyBlobInput:
    model_identifier: str
    target_region: str


@dataclass
class CopyBlobResult:
    model_identifier: str
    source_region: str
    target_region: str
    size_bytes: int
    duration_seconds: float


@dataclass
class SeedBlobInput:
    model_identifier: str  # e.g. "qwen2.5:1.5b"
    target_region: str


@dataclass
class SeedBlobResult:
    model_identifier: str
    region: str
    size_bytes: int
    duration_seconds: float
