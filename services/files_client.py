"""Factory for Azure Files async clients."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from azure.identity.aio import DefaultAzureCredential
from azure.mgmt.storage.aio import StorageManagementClient
from azure.storage.fileshare.aio import ShareServiceClient

from config import get_settings


@asynccontextmanager
async def storage_mgmt_client() -> AsyncGenerator[StorageManagementClient, None]:
    s = get_settings()
    async with DefaultAzureCredential() as credential:
        async with StorageManagementClient(credential, s.azure_subscription_id) as client:
            yield client


@asynccontextmanager
async def files_service_client(storage_account: str, account_key: str) -> AsyncGenerator[ShareServiceClient, None]:
    """Share-level client authenticated with the storage account key."""
    account_url = f"https://{storage_account}.file.core.windows.net"
    async with ShareServiceClient(account_url=account_url, credential=account_key) as client:
        yield client
