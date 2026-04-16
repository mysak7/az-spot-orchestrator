"""Temporal activity for seeding Azure Blob Storage from the Ollama registry.

Downloads all model layers directly from registry.ollama.ai (no VM needed),
packs them into a tar.lz4 archive, and streams the archive directly to Azure
Blob Storage.  Layer data is **never written to disk** — only the manifest
JSON (~KB) uses a temp file.  This allows large models (e.g. 17 GB GGUF) to
be seeded on control-plane VMs with limited free disk space.

Data flow (all running concurrently):
  HTTP download (async coroutine)
    → layer_q  (asyncio.Queue, bounded)
    → tar builder (thread via run_in_executor), reads layer_q via _LayerReader
    → upload_q (asyncio.Queue, bounded)
    → blob upload (asyncio task), reads upload_q via _upload_chunks()
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import tarfile
import tempfile
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import lz4.frame
import httpx
import structlog
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
)
from temporal.types import SeedBlobInput, SeedBlobResult

log = structlog.get_logger()

_REGISTRY = "https://registry.ollama.ai"


async def _fetch_manifest(client: httpx.AsyncClient, name: str, tag: str) -> dict:
    """Fetch model manifest from the Ollama registry (no auth required)."""
    resp = await client.get(
        f"{_REGISTRY}/v2/library/{name}/manifests/{tag}",
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    resp.raise_for_status()
    return resp.json()


async def _stream_to_blob(
    bc: Any,
    http: httpx.AsyncClient,
    manifest: dict,
    name: str,
    tag: str,
    manifests_root: Path,
    model_identifier: str,
    region: str,
    on_progress: Any,
) -> int:
    """Stream registry layers directly into a tar.lz4 blob — zero layer disk writes.

    Returns the number of compressed bytes uploaded.
    """
    loop = asyncio.get_running_loop()

    layers: list[dict] = manifest.get("layers", [])
    config_blob: dict | None = manifest.get("config")
    all_blobs = layers + ([config_blob] if config_blob else [])
    total_bytes = sum(b.get("size", 0) for b in all_blobs)

    # ── Upload queue: sync tar thread → async blob upload ─────────────────────
    upload_q: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(maxsize=8)
    compressed_bytes = 0

    class _UploadWriter(io.RawIOBase):
        def write(self, b: bytes) -> int:  # type: ignore[override]
            f = asyncio.run_coroutine_threadsafe(upload_q.put(bytes(b)), loop)
            try:
                f.result(timeout=60.0)
            except concurrent.futures.TimeoutError:
                # upload_task has likely failed and stopped consuming the queue
                raise IOError("upload queue stalled — blob upload task may have failed")
            return len(b)

        def writable(self) -> bool:
            return True

    async def _upload_chunks() -> AsyncGenerator[bytes, None]:
        nonlocal compressed_bytes
        while True:
            item = await upload_q.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            compressed_bytes += len(item)
            yield item

    # ── Layer queue: async HTTP download → sync tar thread ────────────────────
    # Bounded to ~256 MB (64 × 4 MB chunks); provides backpressure so the
    # downloader doesn't race ahead of the archiver.
    layer_q: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(maxsize=64)

    class _LayerReader(io.RawIOBase):
        """Sync reader used by tarfile.addfile() that blocks on layer_q.get()."""

        def __init__(self) -> None:
            self._buf = b""

        def readinto(self, b: bytearray) -> int:  # type: ignore[override]
            while not self._buf:
                chunk = asyncio.run_coroutine_threadsafe(layer_q.get(), loop).result()
                if chunk is None:
                    return 0  # EOF for this layer
                if isinstance(chunk, Exception):
                    raise chunk
                self._buf = chunk
            n = min(len(b), len(self._buf))
            b[:n] = self._buf[:n]
            self._buf = self._buf[n:]
            return n

        def readable(self) -> bool:
            return True

    # ── Archive builder (runs in a thread) ────────────────────────────────────
    def _build_archive() -> None:
        try:
            writer = _UploadWriter()
            with lz4.frame.open(writer, "wb") as lz4_fh:
                with tarfile.open(fileobj=lz4_fh, mode="w|") as tar:
                    # Manifest JSON files (tiny, already on disk)
                    tar.add(manifests_root, arcname="models/manifests")
                    # Layer blobs — streamed from layer_q, nothing written to disk
                    for blob_info in all_blobs:
                        digest: str = blob_info["digest"]
                        size: int = blob_info.get("size", 0)
                        ti = tarfile.TarInfo(
                            name=f"models/blobs/{digest.replace(':', '-')}"
                        )
                        ti.size = size
                        tar.addfile(ti, _LayerReader())
                        # tarfile reads exactly ti.size bytes and stops; the None
                        # sentinel (and any extra bytes if download > manifest size)
                        # must be drained so the next layer's _LayerReader doesn't
                        # read this layer's sentinel and return premature EOF.
                        while True:
                            item = asyncio.run_coroutine_threadsafe(
                                layer_q.get(), loop
                            ).result()
                            if item is None:
                                break
                            if isinstance(item, Exception):
                                raise item
                            # extra bytes beyond ti.size — discard silently
            asyncio.run_coroutine_threadsafe(upload_q.put(None), loop).result()
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(upload_q.put(exc), loop).result()

    async def _heartbeat_loop() -> None:
        while True:
            activity.heartbeat("streaming tar.lz4 to blob storage")
            await asyncio.sleep(30)

    heartbeat_task = asyncio.create_task(_heartbeat_loop())
    archive_future = loop.run_in_executor(None, _build_archive)
    upload_task = asyncio.ensure_future(
        bc.upload_blob(_upload_chunks(), overwrite=True, blob_type="BlockBlob")
    )

    # ── Layer downloader (feeds layer_q sequentially) ─────────────────────────
    # Layers are processed one at a time to keep memory bounded and simplify
    # coordination with the tar thread (tar is inherently sequential).
    downloaded_bytes = 0
    last_hb_time = 0.0
    last_progress_time = 0.0
    try:
        for blob_info in all_blobs:
            digest = blob_info["digest"]
            activity.heartbeat(f"downloading {digest[:19]}")
            async with http.stream(
                "GET", f"{_REGISTRY}/v2/library/{name}/blobs/{digest}"
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(chunk_size=4 * 1024 * 1024):
                    await layer_q.put(chunk)
                    downloaded_bytes += len(chunk)
                    now = time.monotonic()
                    if now - last_hb_time >= 30.0:
                        activity.heartbeat(f"downloading {digest[:19]}")
                        last_hb_time = now
                    if total_bytes > 0 and now - last_progress_time >= 2.0:
                        last_progress_time = now
                        pct = min(99, int(downloaded_bytes * 100 / total_bytes))
                        await on_progress(pct)
            await layer_q.put(None)  # EOF sentinel for this layer
    except Exception as exc:
        await layer_q.put(exc)  # wake tar thread so it can propagate the error
        raise

    if total_bytes > 0:
        await on_progress(100)

    await archive_future
    await upload_task
    heartbeat_task.cancel()
    return compressed_bytes


@activity.defn
async def seed_blob_from_registry(input: SeedBlobInput) -> SeedBlobResult:
    """Download a model from Ollama registry and upload it to Azure Blob Storage.

    Produces a tar.lz4 with the internal layout that VMs expect:
      models/blobs/sha256-<digest>
      models/manifests/registry.ollama.ai/library/<name>/<tag>

    Layer data is never written to disk — only the manifest JSON (~KB) uses a
    temp file.  This allows large models (e.g. a 17 GB GGUF) to be seeded on
    control-plane VMs with limited free disk space.
    """
    model_identifier = input.model_identifier
    region = input.target_region

    if ":" in model_identifier:
        name, tag = model_identifier.split(":", 1)
    else:
        name, tag = model_identifier, "latest"

    s = get_settings()
    log.info("blob_seed_started", model_identifier=model_identifier, region=region)
    await mark_upload_started(model_identifier, region)

    # Only manifest JSON (~KB) needs a temp dir.
    with tempfile.TemporaryDirectory(prefix="ollama_seed_") as tmpdir:
        manifests_dir = (
            Path(tmpdir) / "registry.ollama.ai" / "library" / name
        )
        manifests_dir.mkdir(parents=True)

        try:
            async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as http:
                # ── Manifest ────────────────────────────────────────────────────
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

                (manifests_dir / tag).write_text(json.dumps(manifest))

                blob = _blob_name(model_identifier, region)
                start = time.monotonic()

                # tmpdir contains registry.ollama.ai/library/<name>/<tag>
                # tar.add(tmpdir, arcname="models/manifests") →
                #   models/manifests/registry.ollama.ai/library/<name>/<tag>
                manifests_root = Path(tmpdir)

                async def _on_progress(pct: int) -> None:
                    await update_download_progress(model_identifier, region, pct)

                async with blob_service_client() as az:
                    bc = az.get_blob_client(
                        container=s.azure_storage_container_name, blob=blob
                    )
                    size_bytes = await _stream_to_blob(
                        bc, http, manifest, name, tag,
                        manifests_root,
                        model_identifier, region, _on_progress,
                    )

            duration = time.monotonic() - start
            await register_upload_complete(
                model_identifier, region, size_bytes, duration
            )
            log.info(
                "blob_seed_complete",
                model_identifier=model_identifier,
                region=region,
                size_mb=round(size_bytes / 1_048_576, 1),
                duration_s=round(duration, 1),
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
