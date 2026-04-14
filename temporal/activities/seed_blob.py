"""Temporal activity for seeding Azure Blob Storage from the Ollama registry.

Downloads all model layers directly from registry.ollama.ai (no VM needed),
packs them into the same tar.gz layout that VMs expect, and uploads to the
target-region blob container.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from config import get_settings
from services.blob_client import blob_service_client
from services.model_cache import (
    _blob_name,
    mark_upload_started,
    register_upload_complete,
    update_upload_phase,
)
from temporal.types import SeedBlobInput, SeedBlobResult

logger = logging.getLogger(__name__)

_REGISTRY = "https://registry.ollama.ai"
_AUTH_URL = "https://ollama.ai/v2/auth"
_DOWNLOAD_CONCURRENCY = 4


async def _get_token(client: httpx.AsyncClient, name: str) -> str:
    """Obtain a Bearer token from the Ollama auth service."""
    resp = await client.get(
        _AUTH_URL,
        params={
            "service": "registry.ollama.ai",
            "scope": f"repository:library/{name}:pull",
        },
    )
    resp.raise_for_status()
    return resp.json()["token"]


async def _fetch_manifest(
    client: httpx.AsyncClient, name: str, tag: str, token: str
) -> dict:
    resp = await client.get(
        f"{_REGISTRY}/v2/library/{name}/manifests/{tag}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.docker.distribution.manifest.v2+json",
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _download_layer(
    client: httpx.AsyncClient,
    name: str,
    digest: str,
    token: str,
    dest: Path,
    sem: asyncio.Semaphore,
) -> None:
    """Stream a single blob layer to *dest* with bounded concurrency."""
    async with sem:
        activity.heartbeat(f"downloading {digest[:19]}")
        async with client.stream(
            "GET",
            f"{_REGISTRY}/v2/library/{name}/blobs/{digest}",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=4 * 1024 * 1024):
                    fh.write(chunk)


def _build_tar(models_dir: Path, tar_path: Path) -> int:
    """Pack *models_dir* into *tar_path* and return the file size in bytes."""
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(models_dir, arcname="models")
    return tar_path.stat().st_size


async def _upload_with_heartbeat(bc: object, tar_path: Path) -> None:
    """Upload tar.gz to blob storage, heartbeating every 30 s so Temporal doesn't time out."""

    async def _heartbeat() -> None:
        while True:
            activity.heartbeat("uploading to blob storage")
            await asyncio.sleep(30)

    task = asyncio.create_task(_heartbeat())
    try:
        with tar_path.open("rb") as fh:
            await bc.upload_blob(fh, overwrite=True, blob_type="BlockBlob")  # type: ignore[union-attr]
    finally:
        task.cancel()


@activity.defn
async def seed_blob_from_registry(input: SeedBlobInput) -> SeedBlobResult:
    """Download a model from Ollama registry and upload it to Azure Blob Storage.

    Produces a tar.gz with the same internal layout as VMs expect:
      models/blobs/sha256-<digest>
      models/manifests/registry.ollama.ai/library/<name>/<tag>

    This lets VMs skip the Ollama pull entirely — they just extract the tar
    and restart Ollama.
    """
    model_identifier = input.model_identifier
    region = input.target_region

    if ":" in model_identifier:
        name, tag = model_identifier.split(":", 1)
    else:
        name, tag = model_identifier, "latest"

    s = get_settings()
    await mark_upload_started(model_identifier, region)
    await update_upload_phase(model_identifier, region, "pulling")

    tmpdir = Path(tempfile.mkdtemp(prefix="ollama_seed_"))
    try:
        async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as http:
            # ── Auth ────────────────────────────────────────────────────────────
            activity.heartbeat("authenticating with Ollama registry")
            try:
                token = await _get_token(http, name)
            except Exception as exc:
                raise ApplicationError(
                    f"Ollama registry auth failed for '{name}': {exc}",
                    non_retryable=True,
                ) from exc

            # ── Manifest ────────────────────────────────────────────────────────
            activity.heartbeat("fetching manifest")
            try:
                manifest = await _fetch_manifest(http, name, tag, token)
            except httpx.HTTPStatusError as exc:
                raise ApplicationError(
                    f"Manifest not found for '{model_identifier}' "
                    f"(HTTP {exc.response.status_code}). "
                    "Check the model identifier (e.g. 'qwen2.5:1.5b').",
                    non_retryable=True,
                ) from exc

            layers: list[dict] = manifest.get("layers", [])
            config: dict | None = manifest.get("config")
            all_blobs = layers + ([config] if config else [])

            # ── Directory structure ─────────────────────────────────────────────
            blobs_dir = tmpdir / "models" / "blobs"
            blobs_dir.mkdir(parents=True)
            manifests_dir = (
                tmpdir / "models" / "manifests" / "registry.ollama.ai" / "library" / name
            )
            manifests_dir.mkdir(parents=True)
            (manifests_dir / tag).write_text(json.dumps(manifest))

            # ── Download layers ─────────────────────────────────────────────────
            sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
            tasks = [
                _download_layer(
                    http,
                    name,
                    blob["digest"],
                    token,
                    blobs_dir / blob["digest"].replace(":", "-"),
                    sem,
                )
                for blob in all_blobs
            ]
            try:
                await asyncio.gather(*tasks)
            except Exception as exc:
                raise ApplicationError(
                    f"Layer download failed for '{model_identifier}': {exc}"
                ) from exc

        # ── Archive ─────────────────────────────────────────────────────────────
        await update_upload_phase(model_identifier, region, "archiving")
        activity.heartbeat("building tar.gz")
        tar_path = tmpdir / "model.tar.gz"
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        size_bytes = await loop.run_in_executor(
            None, _build_tar, tmpdir / "models", tar_path
        )

        # ── Upload ──────────────────────────────────────────────────────────────
        await update_upload_phase(model_identifier, region, "uploading")
        blob = _blob_name(model_identifier, region)
        async with blob_service_client() as az:
            bc = az.get_blob_client(container=s.azure_storage_container_name, blob=blob)
            await _upload_with_heartbeat(bc, tar_path)

        duration = time.monotonic() - start
        await register_upload_complete(model_identifier, region, size_bytes, duration)

        logger.info(
            "Seeded blob %s in %s: %.1f MB in %.1fs",
            model_identifier,
            region,
            size_bytes / 1_048_576,
            duration,
        )
        return SeedBlobResult(
            model_identifier=model_identifier,
            region=region,
            size_bytes=size_bytes,
            duration_seconds=duration,
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
