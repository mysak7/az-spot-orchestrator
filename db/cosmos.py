"""Azure Cosmos DB client and container accessors.

A single CosmosClient is created lazily and reused across requests.
`setup_cosmos()` is called once at startup to ensure the database and
containers exist.
"""
from __future__ import annotations

from azure.cosmos import PartitionKey
from azure.cosmos.aio import ContainerProxy, CosmosClient

from config import get_settings

_DB_NAME = "az-spot-orchestrator"
MODELS_CONTAINER = "llm_models"
INSTANCES_CONTAINER = "vm_instances"

_client: CosmosClient | None = None


def _get_client() -> CosmosClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = CosmosClient(url=s.cosmos_endpoint, credential=s.cosmos_key)
    return _client


def get_models_container() -> ContainerProxy:
    return _get_client().get_database_client(_DB_NAME).get_container_client(MODELS_CONTAINER)


def get_instances_container() -> ContainerProxy:
    return _get_client().get_database_client(_DB_NAME).get_container_client(INSTANCES_CONTAINER)


async def setup_cosmos() -> None:
    """Idempotently create the Cosmos DB database and containers."""
    client = _get_client()
    db = await client.create_database_if_not_exists(id=_DB_NAME)
    for container_id in (MODELS_CONTAINER, INSTANCES_CONTAINER):
        await db.create_container_if_not_exists(
            id=container_id,
            partition_key=PartitionKey(path="/id"),
        )
