"""Factories for authenticated Azure SDK async clients."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from azure.identity.aio import ClientSecretCredential
from azure.mgmt.compute.aio import ComputeManagementClient
from azure.mgmt.network.aio import NetworkManagementClient

from config import get_settings


def _credential() -> ClientSecretCredential:
    s = get_settings()
    return ClientSecretCredential(
        tenant_id=s.azure_tenant_id,
        client_id=s.azure_client_id,
        client_secret=s.azure_client_secret,
    )


@asynccontextmanager
async def compute_client() -> AsyncGenerator[ComputeManagementClient, None]:
    s = get_settings()
    cred = _credential()
    async with ComputeManagementClient(cred, s.azure_subscription_id) as client:
        yield client


@asynccontextmanager
async def network_client() -> AsyncGenerator[NetworkManagementClient, None]:
    s = get_settings()
    cred = _credential()
    async with NetworkManagementClient(cred, s.azure_subscription_id) as client:
        yield client
