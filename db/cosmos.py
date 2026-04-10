"""Azure Cosmos DB client and container accessors.

A single CosmosClient is created lazily and reused across requests.
`setup_cosmos()` is called once at startup to ensure the database and
containers exist.  `seed_default_models()` then upserts any models listed in
`config.DEFAULT_MODELS` that are not yet registered.
"""
from __future__ import annotations

import logging

from azure.cosmos import PartitionKey
from azure.cosmos.aio import ContainerProxy, CosmosClient

from config import DEFAULT_MODELS, get_settings
from db.models import LLMModel

logger = logging.getLogger(__name__)

_DB_NAME = "az-spot-orchestrator"
MODELS_CONTAINER = "llm_models"
INSTANCES_CONTAINER = "vm_instances"
MODEL_CACHE_CONTAINER = "model_cache"

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


def get_cache_container() -> ContainerProxy:
    return _get_client().get_database_client(_DB_NAME).get_container_client(MODEL_CACHE_CONTAINER)


async def setup_cosmos() -> None:
    """Idempotently create the Cosmos DB database and containers."""
    client = _get_client()
    db = await client.create_database_if_not_exists(id=_DB_NAME)
    for container_id in (MODELS_CONTAINER, INSTANCES_CONTAINER, MODEL_CACHE_CONTAINER):
        await db.create_container_if_not_exists(
            id=container_id,
            partition_key=PartitionKey(path="/id"),
        )


async def seed_default_models() -> None:
    """Register any DEFAULT_MODELS not yet present in the model registry.

    Checks by `name` (unique slug) so re-running on restart is safe.
    """
    if not DEFAULT_MODELS:
        return

    container = get_models_container()

    # Fetch all existing names in one query to avoid N point-reads.
    existing_names: set[str] = {
        item["name"]
        async for item in container.query_items(
            query="SELECT c.name FROM c",
        )
    }

    for seed in DEFAULT_MODELS:
        if seed.name in existing_names:
            logger.debug("Seed model '%s' already registered – skipping.", seed.name)
            continue

        model = LLMModel(
            name=seed.name,
            model_identifier=seed.model_identifier,
            size_mb=seed.size_mb,
            vm_size=seed.vm_size,
            description=seed.description,
        )
        await container.create_item(body=model.model_dump())
        logger.info("Seeded default model '%s' (%s).", seed.name, seed.model_identifier)
