"""Azure Cosmos DB client and container accessors.

A single CosmosClient is created lazily and reused across requests.
`setup_cosmos()` is called once at startup to ensure the database and
containers exist.  `seed_default_models()` then upserts any models listed in
`config.DEFAULT_MODELS` that are not yet registered.
"""

from __future__ import annotations

import logging

from azure.cosmos.aio import ContainerProxy, CosmosClient
from azure.identity.aio import DefaultAzureCredential

from config import DEFAULT_MODELS, get_settings
from db.models import LLMModel

logger = logging.getLogger(__name__)

_DB_NAME = "az-spot-orchestrator"
MODELS_CONTAINER = "llm-models"
INSTANCES_CONTAINER = "vm-instances"
MODEL_CACHE_CONTAINER = "model-cache"

_client: CosmosClient | None = None


def _get_client() -> CosmosClient:
    global _client
    if _client is None:
        s = get_settings()
        if not s.cosmos_endpoint:
            raise RuntimeError(
                "COSMOS_ENDPOINT is not configured. "
                "Set it in your .env file or environment variables."
            )
        _client = CosmosClient(url=s.cosmos_endpoint, credential=DefaultAzureCredential())
    return _client


def get_models_container() -> ContainerProxy:
    return _get_client().get_database_client(_DB_NAME).get_container_client(MODELS_CONTAINER)


def get_instances_container() -> ContainerProxy:
    return _get_client().get_database_client(_DB_NAME).get_container_client(INSTANCES_CONTAINER)


def get_cache_container() -> ContainerProxy:
    return _get_client().get_database_client(_DB_NAME).get_container_client(MODEL_CACHE_CONTAINER)


async def setup_cosmos() -> None:
    """Verify Cosmos DB connectivity.

    Schema (database + containers) is managed by Terraform, not the app.
    This just confirms the connection and AAD auth work at startup.
    Skips silently when COSMOS_ENDPOINT is not configured.
    """
    s = get_settings()
    if not s.cosmos_endpoint:
        logger.warning("COSMOS_ENDPOINT not set — skipping Cosmos DB connectivity check")
        return
    try:
        client = _get_client()
        async for _ in client.list_databases():
            break
    except Exception as exc:
        logger.warning("Cosmos DB connectivity check failed: %s", exc)


async def seed_default_models() -> None:
    """Register any DEFAULT_MODELS not yet present in the model registry.

    Checks by `name` (unique slug) so re-running on restart is safe.
    Skips silently when COSMOS_ENDPOINT is not configured.
    """
    s = get_settings()
    if not DEFAULT_MODELS or not s.cosmos_endpoint:
        return

    try:
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
    except Exception as exc:
        logger.warning("Failed to seed default models: %s", exc)
