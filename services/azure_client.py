"""Factories for authenticated Azure SDK async clients."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from azure.identity.aio import DefaultAzureCredential
from azure.mgmt.compute.aio import ComputeManagementClient
from azure.mgmt.network.aio import NetworkManagementClient

from config import get_settings


@asynccontextmanager
async def compute_client() -> AsyncGenerator[ComputeManagementClient, None]:
    s = get_settings()
    async with DefaultAzureCredential() as credential:
        async with ComputeManagementClient(credential, s.azure_subscription_id) as client:
            yield client


@asynccontextmanager
async def network_client() -> AsyncGenerator[NetworkManagementClient, None]:
    s = get_settings()
    async with DefaultAzureCredential() as credential:
        async with NetworkManagementClient(credential, s.azure_subscription_id) as client:
            yield client
