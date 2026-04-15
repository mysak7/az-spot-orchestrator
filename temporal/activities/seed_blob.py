"""Temporal activity for seeding Azure Blob Storage from the Ollama registry.

Downloads all model layers directly from registry.ollama.ai (no VM needed),
packs them into a tar.lz4 archive (lz4 is much faster than gzip for already-
compressed GGUF weights), and streams the archive directly to Azure Blob Storage
without writing it to disk — allowing large models to be seeded on machines with
limited free disk space (only the downloaded layers need to fit, not layers + archive).
"""

from __future__ import annotations

import asyncio
import io
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
    Heartbeats are sent every 30 s during streaming so large layers (e.g. a
    single 15 GB GGUF) don't exceed the 5-minute heartbeat_timeout.
    """
    async with sem:
        activity.heartbeat(f"downloading {digest[:19]}")
        async with client.stream(
            "GET",
            f"{_REGISTRY}/v2/library/{name}/blobs/{digest}",
        ) as resp:
            resp.raise_for_status()
            last_heartbeat = time.monotonic()
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=4 * 1024 * 1024):
                    fh.write(chunk)
                    now = time.monotonic()
                    if now - last_heartbeat >= 30.0:
                        activity.heartbeat(f"downloading {digest[:19]}")
                        last_heartbeat = now
                    if on_bytes is not None:
                        await on_bytes(len(chunk))


async def _stream_archive_upload(bc: object, models_dir: Path) -> int:
    """Stream a tar.lz4 archive of *models_dir* directly to Azure Blob Storage.

    The archive is created in a thread and piped to the uploader via an asyncio
    Queue — no temp file is written.  This means only the downloaded layer files
    need to fit on disk (not layers + archive), halving the required free space
    for large models.

    Returns the number of compressed bytes uploaded (the blob size).
    """
    loop = asyncio.get_running_loop()
    # Sentinel for end-of-stream; exceptions are forwarded through the queue too.
    queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(maxsize=8)
    size_bytes = 0

    class _QueueWriter(io.RawIOBase):
        """Synchronous writer that forwards lz4-compressed chunks to the async queue."""

        def write(self, b: bytes | bytearray | memoryview) -> int:  # type: ignore[override]
            chunk = bytes(b)
            asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            return len(chunk)

        def writable(self) -> bool:
            return True

    def _build_archive() -> None:
        try:
            writer = _QueueWriter()
            with lz4.frame.open(writer, "wb") as lz4_fh:
                with tarfile.open(fileobj=lz4_fh, mode="w|") as tar:
                    tar.add(models_dir, arcname="models")
            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()

    async def _chunks() -> Any:
        nonlocal size_bytes
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            size_bytes += len(item)
            yield item

    async def _heartbeat_loop() -> None:
        while True:
            activity.heartbeat("streaming tar.lz4 to blob storage")
            await asyncio.sleep(30)

    heartbeat_task = asyncio.create_task(_heartbeat_loop())
    archive_future = loop.run_in_executor(None, _build_archive)
    try:
        await bc.upload_blob(_chunks(), overwrite=True, blob_type="BlockBlob")  # type: ignore[attr-defined]
    finally:
        heartbeat_task.cancel()

    await archive_future  # surface any exception from the archive thread
    return size_bytes


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

        # ── Stream archive directly to Azure (no temp file needed) ─────────────
        # Streaming eliminates the need for a second copy of the model on disk —
        # only the downloaded layers (~model size) need to fit, not layers + archive.
        await update_upload_phase(model_identifier, region, "uploading")
        blob = _blob_name(model_identifier, region)
        start = time.monotonic()
        async with blob_service_client() as az:
            bc = az.get_blob_client(container=s.azure_storage_container_name, blob=blob)
            size_bytes = await _stream_archive_upload(bc, tmpdir / "models")

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
