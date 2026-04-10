#!/usr/bin/env python3
"""Quick smoke-test: send a chat request through the control-plane proxy
and print the response.  Requires the FastAPI server to be running."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx

DEFAULT_BASE_URL = "http://74.241.243.18:8000"
MODEL_NAME = "qwen2-5-1-5b"       # model_name as registered in /api/models
OLLAMA_MODEL = "qwen2.5:1.5b"     # Ollama tag sent in the request body
PROMPT = "In one sentence, what is the capital of France?"


async def main(base_url: str) -> None:
    BASE_URL = base_url.rstrip("/")
    print(f"Target : {BASE_URL}/proxy/{MODEL_NAME}/v1/chat/completions")
    print(f"Prompt : {PROMPT}\n")

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
    }

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=120.0) as client:
        # 1. Check control plane is up
        try:
            health = await client.get(f"{BASE_URL}/health")
            health.raise_for_status()
        except Exception as exc:
            print(f"[ERROR] Control plane not reachable: {exc}")
            sys.exit(1)

        # 2. Check a running instance exists
        instances_resp = await client.get(f"{BASE_URL}/api/models")
        models = instances_resp.json() if instances_resp.status_code == 200 else []
        model_entry = next((m for m in models if m["name"] == MODEL_NAME), None)
        if model_entry:
            inst_resp = await client.get(
                f"{BASE_URL}/api/models/{model_entry['id']}/instances"
            )
            instances = inst_resp.json() if inst_resp.status_code == 200 else []
            running = [i for i in instances if i["status"] == "running"]
            if running:
                print(f"[OK] Running instance: {running[0]['vm_name']} @ {running[0].get('ip_address')} ({running[0].get('region')})")
            else:
                statuses = [i["status"] for i in instances] or ["none"]
                print(f"[WARN] No running instance found (statuses: {statuses})")
        else:
            print(f"[WARN] Model '{MODEL_NAME}' not found in registry")

        # 3. Send the chat request through the proxy
        print("\nSending chat request...")
        try:
            resp = await client.post(
                f"{BASE_URL}/proxy/{MODEL_NAME}/v1/chat/completions",
                json=payload,
            )
        except httpx.RequestError as exc:
            print(f"[ERROR] Request failed: {exc}")
            sys.exit(1)

    elapsed = time.monotonic() - t0

    print(f"HTTP status : {resp.status_code}  ({elapsed:.1f}s)")

    if resp.status_code != 200:
        print(f"[ERROR] {resp.text}")
        sys.exit(1)

    data = resp.json()
    reply = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "<no content>")
    )
    usage = data.get("usage", {})

    print(f"Reply       : {reply}")
    if usage:
        print(f"Tokens      : prompt={usage.get('prompt_tokens')}  completion={usage.get('completion_tokens')}  total={usage.get('total_tokens')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke-test the LLM proxy")
    parser.add_argument(
        "--url",
        default=DEFAULT_BASE_URL,
        help=f"Base URL of the control plane (default: {DEFAULT_BASE_URL})",
    )
    args = parser.parse_args()
    asyncio.run(main(args.url))
