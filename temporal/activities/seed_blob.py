"""Temporal activity for seeding Azure Blob Storage from the Ollama registry.

Downloads all model layers directly from registry.ollama.ai (no VM needed),
packs them into a tar.lz4 archive (lz4 is much faster than gzip for already-
compressed GGUF weights), and uploads to the target-region blob container.
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
from typing import Any

import lz4.frame

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from config import get_settings
from services.blob_client import blob_service_client
from services.model_cache import (
    _blob_name,
    mark_upload_failed,
    mark_upload_started,
    register_upload_complete,
    update_download_progress,
    update_upload_phase,
)
from temporal.types import SeedBlobInput, SeedBlobResult

logger = logging.getLogger(__name__)

_REGISTRY = "https://registry.ollama.ai"
_DOWNLOAD_CONCURRENCY = 4


async def _fetch_manifest(client: httpx.AsyncClient, name: str, tag: str) -> dict:
    """Fetch model manifest from the Ollama registry (no auth required)."""
    resp = await client.get(
        f"{_REGISTRY}/v2/library/{name}/manifests/{tag}",
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    resp.raise_for_status()
    return resp.json()


async def _download_layer(
    client: httpx.AsyncClient,
    name: str,
    digest: str,
    dest: Path,
    sem: asyncio.Semaphore,
    on_bytes: Any | None = None,
) -> None:
    """Stream a single blob layer to *dest* with bounded concurrency.

    *on_bytes* is an optional async callable(n_bytes) called after each chunk.
    """
    async with sem:
        activity.heartbeat(f"downloading {digest[:19]}")
        async with client.stream(
            "GET",
            f"{_REGISTRY}/v2/library/{name}/blobs/{digest}",
        ) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=4 * 1024 * 1024):
                    fh.write(chunk)
                    if on_bytes is not None:
                        await on_bytes(len(chunk))


def _build_tar_lz4(models_dir: Path, tar_lz4_path: Path) -> int:
    """Pack *models_dir* into an lz4-compressed tar and return the file size in bytes.

    lz4 is orders of magnitude faster than gzip for this workload — GGUF model
    weights are already in a semi-compressed format, so gzip gains very little
    while consuming significant CPU time (minutes for 20 GB models).
    """
    with lz4.frame.open(tar_lz4_path, "wb") as lz4_fh:
        with tarfile.open(fileobj=lz4_fh, mode="w|") as tar:
            tar.add(models_dir, arcname="models")
    return tar_lz4_path.stat().st_size


async def _archive_with_heartbeat(models_dir: Path, tar_lz4_path: Path) -> int:
    """Run lz4 tar creation in a thread pool while heartbeating so Temporal doesn't time out.

    Without heartbeating during this phase, large models (e.g. 20 GB) can exceed
    the 5-minute heartbeat_timeout and be incorrectly killed by Temporal.
    """

    async def _heartbeat_loop() -> None:
        while True:
            activity.heartbeat("archiving model files (lz4)")
            await asyncio.sleep(30)

    task = asyncio.create_task(_heartbeat_loop())
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _build_tar_lz4, models_dir, tar_lz4_path)
    finally:
        task.cancel()


async def _upload_with_heartbeat(bc: object, tar_path: Path) -> None:
    """Upload blob to Azure Storage, heartbeating every 30 s so Temporal doesn't time out."""

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

    Produces a tar.lz4 with the internal layout that VMs expect:
      models/blobs/sha256-<digest>
      models/manifests/registry.ollama.ai/library/<name>/<tag>

    This lets VMs skip the Ollama pull entirely — they just extract the archive
    and restart Ollama.  lz4 is used instead of gzip because GGUF weights don't
    compress well with gzip (< 5% smaller) but gzip creation takes minutes for
    large models, causing Temporal heartbeat timeouts.
    """
    model_identifier = input.model_identifier
    region = input.target_region

    if ":" in model_identifier:
        name, tag = model_identifier.split(":", 1)
    else:
        name, tag = model_identifier, "latest"

    s = get_settings()
    await mark_upload_started(model_identifier, region)

    tmpdir = Path(tempfile.mkdtemp(prefix="ollama_seed_"))
    try:
        async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as http:
            # ── Manifest ────────────────────────────────────────────────────────
            activity.heartbeat("fetching manifest")
            try:
                manifest = await _fetch_manifest(http, name, tag)
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
            total_bytes = sum(b.get("size", 0) for b in all_blobs)
            downloaded_bytes = 0
            progress_lock = asyncio.Lock()
            last_progress_time = 0.0

            async def _on_bytes(n: int) -> None:
                nonlocal downloaded_bytes, last_progress_time
                async with progress_lock:
                    downloaded_bytes += n
                    now = time.monotonic()
                    if now - last_progress_time >= 2.0 and total_bytes > 0:
                        last_progress_time = now
                        pct = min(99, int(downloaded_bytes * 100 / total_bytes))
                        await update_download_progress(model_identifier, region, pct)

            sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
            tasks = [
                _download_layer(
                    http,
                    name,
                    blob["digest"],
                    blobs_dir / blob["digest"].replace(":", "-"),
                    sem,
                    on_bytes=_on_bytes,
                )
                for blob in all_blobs
            ]
            try:
                await asyncio.gather(*tasks)
            except Exception as exc:
                raise ApplicationError(
                    f"Layer download failed for '{model_identifier}': {exc}"
                ) from exc
            if total_bytes > 0:
                await update_download_progress(model_identifier, region, 100)

        # ── Archive ─────────────────────────────────────────────────────────────
        await update_upload_phase(model_identifier, region, "archiving")
        tar_lz4_path = tmpdir / "model.tar.lz4"
        start = time.monotonic()
        size_bytes = await _archive_with_heartbeat(tmpdir / "models", tar_lz4_path)

        # ── Upload ──────────────────────────────────────────────────────────────
        await update_upload_phase(model_identifier, region, "uploading")
        blob = _blob_name(model_identifier, region)
        async with blob_service_client() as az:
            bc = az.get_blob_client(container=s.azure_storage_container_name, blob=blob)
            await _upload_with_heartbeat(bc, tar_lz4_path)

        duration = time.monotonic() - start
        await register_upload_complete(model_identifier, region, size_bytes, duration)

        logger.info(
            "Seeded lz4 blob %s in %s: %.1f MB in %.1fs",
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

    except Exception:
        await mark_upload_failed(model_identifier, region)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
