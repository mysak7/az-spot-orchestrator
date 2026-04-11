#!/usr/bin/env python3
"""Test script to call the Azure Spot LLM Orchestrator API."""

import asyncio
import json

import httpx

BASE_URL = "http://localhost:8000"

# Example question for the LLM
EXAMPLE_QUESTION = {
    "messages": [{"role": "user", "content": "What is the capital of France?"}],
    "model": "llama3-8b",
    "temperature": 0.7,
}


async def main():
    async with httpx.AsyncClient(timeout=300.0) as client:
        print("=" * 60)
        print("Azure Spot LLM Orchestrator - API Test")
        print("=" * 60)

        # 1. Health check
        print("\n[1] Health Check")
        resp = await client.get(f"{BASE_URL}/health")
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.json()}")

        # 2. Create a model
        print("\n[2] Register a Model")
        model_payload = {
            "name": "llama3-8b",
            "description": "Llama 3 8B parameter model",
            "size_mb": 4800,
            "model_identifier": "llama3:8b",
            "vm_size": "Standard_NC4as_T4_v3",
        }
        resp = await client.post(f"{BASE_URL}/api/models", json=model_payload)
        print(f"Status: {resp.status_code}")
        model_data = resp.json()
        print(f"Response: {json.dumps(model_data, indent=2)}")

        if resp.status_code not in (201, 409):  # 409 if already exists
            print("Failed to create model")
            return

        model_id = model_data.get("id")
        print(f"Model ID: {model_id}")

        # 3. List models
        print("\n[3] List Models")
        resp = await client.get(f"{BASE_URL}/api/models")
        print(f"Status: {resp.status_code}")
        models = resp.json()
        print(f"Total models: {len(models)}")
        for model in models:
            print(f"  - {model['name']} ({model['model_identifier']})")

        # 4. Get specific model
        print(f"\n[4] Get Model Details ({model_id})")
        resp = await client.get(f"{BASE_URL}/api/models/{model_id}")
        print(f"Status: {resp.status_code}")
        print(f"Response: {json.dumps(resp.json(), indent=2)}")

        # 5. Provision a VM
        print("\n[5] Provision VM for Model")
        provision_payload = {
            "vm_size": "Standard_NC4as_T4_v3",
        }
        resp = await client.post(
            f"{BASE_URL}/api/models/{model_id}/provision",
            json=provision_payload,
        )
        print(f"Status: {resp.status_code}")
        provision_data = resp.json()
        print(f"Response: {json.dumps(provision_data, indent=2)}")

        _vm_name = provision_data.get("vm_name")
        _workflow_id = provision_data.get("workflow_id")

        # 6. List instances
        print("\n[6] List Instances for Model")
        resp = await client.get(f"{BASE_URL}/api/models/{model_id}/instances")
        print(f"Status: {resp.status_code}")
        instances = resp.json()
        print(f"Total instances: {len(instances)}")
        for instance in instances:
            print(f"  - {instance['vm_name']} (status: {instance['status']})")

        # 7. Check instance status after a short wait
        print("\n[7] Check Instance Status (after 2 sec)")
        await asyncio.sleep(2)
        resp = await client.get(f"{BASE_URL}/api/models/{model_id}/instances")
        if resp.status_code == 200:
            instances = resp.json()
            if instances:
                latest = instances[0]
                print(f"Latest instance status: {latest['status']}")
                print(f"IP Address: {latest.get('ip_address', 'Not assigned yet')}")
                print(f"Region: {latest.get('region', 'Not assigned yet')}")

        # 8. Proxy request (will fail if VM not ready, which is expected in test)
        print("\n[8] Example Proxy Request (LLM Inference)")
        print("Model: llama3-8b")
        print(f"Question: {EXAMPLE_QUESTION['messages'][0]['content']}")
        print("Note: This will fail if no VM is running yet (expected in test environment)")

        resp = await client.post(
            f"{BASE_URL}/proxy/llama3-8b/v1/chat/completions",
            json=EXAMPLE_QUESTION,
        )
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            print(f"Response: {json.dumps(resp.json(), indent=2)}")
        else:
            print(f"Error: {resp.text}")

        print("\n" + "=" * 60)
        print("Test Complete")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
