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


async def test_model(client: httpx.AsyncClient, base_url: str, model: dict) -> dict:
    model_name = model["name"]
    ollama_model = model["model_identifier"]
    result = {"model": model_name, "ollama": ollama_model, "instance": None, "status_code": None, "reply": None, "elapsed": None, "error": None}

    # Check running instances
    inst_resp = await client.get(f"{base_url}/api/models/{model['id']}/instances")
    instances = inst_resp.json() if inst_resp.status_code == 200 else []
    running = [i for i in instances if i["status"] == "running"]
    if running:
        result["instance"] = f"{running[0]['vm_name']} @ {running[0].get('ip_address')} ({running[0].get('region')})"
    else:
        statuses = [i["status"] for i in instances] or ["none"]
        result["instance"] = f"[no running instance] statuses={statuses}"

    # Skip chat if no running instance
    if not running:
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

    return result


async def main(base_url: str) -> None:
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
        print(f"Found {len(models)} model(s) in catalog — testing in parallel...\n")

        # Test all models in parallel
        tasks = [test_model(client, base_url, m) for m in models]
        results = await asyncio.gather(*tasks)

    # Print results
    ok = 0
    tested = 0
    for r in results:
        sep = "-" * 60
        print(sep)
        print(f"Model    : {r['model']}  ({r['ollama']})")
        print(f"Instance : {r['instance']}")
        if r["status_code"] is None and r["error"] is None:
            print("[SKIP]   : no running instance")
        elif r["error"] is not None:
            tested += 1
            print(f"[ERROR]  : {r['error']}")
        else:
            tested += 1
            status_tag = "[OK]" if r["status_code"] == 200 else "[FAIL]"
            elapsed = f"{r['elapsed']:.1f}s" if r["elapsed"] is not None else "?"
            print(f"HTTP     : {r['status_code']}  ({elapsed})  {status_tag}")
            if r["reply"]:
                print(f"Reply    : {r['reply']}")
                ok += 1
    print("-" * 60)
    skipped = len(results) - tested
    print(f"\n{ok}/{tested} tested model(s) responded successfully  ({skipped} skipped — no instance).")
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
