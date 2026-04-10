"""Factories for authenticated Azure Blob Storage async client."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from azure.storage.blob.aio import BlobServiceClient

from config import get_settings


@asynccontextmanager
async def blob_service_client() -> AsyncGenerator[BlobServiceClient, None]:
    """Create and yield an authenticated BlobServiceClient for model cache."""
    s = get_settings()
    account_url = f"https://{s.azure_storage_account_name}.blob.core.windows.net"
    async with BlobServiceClient(
        account_url=account_url,
        credential=s.azure_storage_account_key,
    ) as client:
        yield client
