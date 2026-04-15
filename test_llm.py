#!/usr/bin/env python3
"""Smoke-test: check all models in the catalog and send a chat request through
the control-plane proxy for each one — all models tested in parallel.
Requires the FastAPI server to be running."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

DEFAULT_BASE_URL = "http://135.225.121.221"
PROMPT = "In one sentence, what is the capital of France?"

_total_start: float = 0.0


def _ts() -> str:
    """Elapsed time since test start, e.g. '+12.3s'."""
    return f"+{time.monotonic() - _total_start:5.1f}s"


async def test_model(
    client: httpx.AsyncClient,
    base_url: str,
    model: dict,
    print_lock: asyncio.Lock,
    pending: list[int],
) -> dict:
    model_name = model["name"]
    ollama_model = model["model_identifier"]
    result = {
        "model": model_name,
        "ollama": ollama_model,
        "instance": None,
        "status_code": None,
        "reply": None,
        "elapsed": None,
        "error": None,
    }

    # Check running instances
    inst_resp = await client.get(f"{base_url}/api/models/{model['id']}/instances")
    instances = inst_resp.json() if inst_resp.status_code == 200 else []
    running = [i for i in instances if i["status"] == "running"]
    if running:
        result["instance"] = (
            f"{running[0]['vm_name']} @ {running[0].get('ip_address')} ({running[0].get('region')})"
        )
    else:
        statuses = [i["status"] for i in instances] or ["none"]
        result["instance"] = f"[no running instance] statuses={statuses}"

    # Skip chat if no running instance
    if not running:
        async with print_lock:
            pending[0] -= 1
            left = pending[0]
            sep = "-" * 60
            print(f"\n{sep}")
            print(f"{_ts()}  Model    : {model_name}  ({ollama_model})")
            print(f"          Instance : {result['instance']}")
            print(f"          [SKIP]   : no running instance")
            if left:
                print(f"          ({left} still running...)")
            sys.stdout.flush()
        return result

    # Send chat request via native Ollama API (supports think:false reliably)
    payload = {
        "model": ollama_model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "think": False,
        "options": {"num_predict": 20},
    }
    t0 = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/proxy/{model_name}/api/chat",
            json=payload,
        )
        result["elapsed"] = time.monotonic() - t0
        result["status_code"] = resp.status_code
        if resp.status_code == 200:
            data = resp.json()
            result["reply"] = data.get("message", {}).get("content", "<no content>")
        else:
            result["error"] = resp.text[:200]
    except httpx.RequestError as exc:
        result["elapsed"] = time.monotonic() - t0
        result["error"] = f"{type(exc).__name__}: {exc}" or type(exc).__name__

    async with print_lock:
        pending[0] -= 1
        left = pending[0]
        sep = "-" * 60
        print(f"\n{sep}")
        print(f"{_ts()}  Model    : {model_name}  ({ollama_model})")
        print(f"          Instance : {result['instance']}")
        if result["error"] is not None:
            print(f"          [ERROR]  : {result['error']}")
        else:
            status_tag = "[OK]" if result["status_code"] == 200 else "[FAIL]"
            elapsed = f"{result['elapsed']:.1f}s" if result["elapsed"] is not None else "?"
            print(f"          HTTP     : {result['status_code']}  ({elapsed})  {status_tag}")
            if result["reply"]:
                print(f"          Reply    : {result['reply']}")
        if left:
            print(f"          ({left} still running...)")
        sys.stdout.flush()

    return result


async def _ticker(stop: asyncio.Event) -> None:
    """Print a heartbeat line every 5s while tasks are running."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            print(f"  {_ts()}  ... still waiting ...", flush=True)


async def main(base_url: str) -> None:
    global _total_start
    _total_start = time.monotonic()

    base_url = base_url.rstrip("/")
    print(f"Control plane : {base_url}")
    print(f"Prompt        : {PROMPT}\n")

    async with httpx.AsyncClient(timeout=240.0) as client:
        # Health check
        try:
            health = await client.get(f"{base_url}/health")
            health.raise_for_status()
        except Exception as exc:
            print(f"[ERROR] Control plane not reachable: {exc}")
            sys.exit(1)

        # Fetch catalog
        catalog_resp = await client.get(f"{base_url}/api/models")
        if catalog_resp.status_code != 200:
            print(f"[ERROR] Failed to fetch model catalog: {catalog_resp.status_code}")
            sys.exit(1)
        models = catalog_resp.json()
        if not models:
            print("[WARN] No models registered in catalog.")
            return
        print(f"Found {len(models)} model(s) in catalog — firing in parallel...\n")
        for m in models:
            print(f"  queued  {m['name']}  ({m['model_identifier']})")
        print()
        sys.stdout.flush()

        # Test all models in parallel, printing each result as it arrives
        print_lock = asyncio.Lock()
        pending = [len(models)]  # mutable counter shared across coroutines
        stop_ticker = asyncio.Event()

        ticker = asyncio.create_task(_ticker(stop_ticker))
        tasks = [
            asyncio.create_task(test_model(client, base_url, m, print_lock, pending))
            for m in models
        ]
        results = await asyncio.gather(*tasks)
        stop_ticker.set()
        await ticker

    # Summary
    ok = 0
    tested = 0
    for r in results:
        if r["status_code"] is None and r["error"] is None:
            pass  # skipped
        elif r["error"] is not None:
            tested += 1
        else:
            tested += 1
            if r["reply"]:
                ok += 1

    total_elapsed = time.monotonic() - _total_start
    skipped = len(results) - tested
    print(f"\n{'=' * 60}")
    print(f"  {ok}/{tested} tested model(s) OK  |  {skipped} skipped  |  total {total_elapsed:.1f}s")
    print(f"{'=' * 60}")
    if tested > 0 and ok < tested:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parallel smoke-test for all catalog models")
    parser.add_argument(
        "--url",
        default=DEFAULT_BASE_URL,
        help=f"Base URL of the control plane (default: {DEFAULT_BASE_URL})",
    )
    args = parser.parse_args()
    asyncio.run(main(args.url))
