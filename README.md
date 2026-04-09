# Azure Spot LLM Orchestrator

A control plane that dynamically provisions Azure Spot VMs to serve LLMs at minimal cost. It finds the cheapest Azure region, spins up a Spot VM, installs Ollama via cloud-init, and routes inference traffic to it. On eviction, it automatically re-provisions in a new region.

## Architecture

```
  You
   │  OpenAI-compatible request
   ▼
┌──────────────────────────────────────────────┐
│  Control Plane VM  (stable, Standard_D2s_v3) │
│                                              │
│  FastAPI :8000 ──► proxy to active Spot VM   │
│  Temporal :7233 / UI :8080                   │
│  Cosmos DB (serverless)                      │
└──────────────┬───────────────────────────────┘
               │ provisions / re-provisions
               ▼
          Spot VM  (cheapest region)
          Ollama :11434
          /mnt/resource  ← free temp disk for weights
```

## Quick start

1. **Provision infrastructure** — `cd terraform && terraform init && terraform apply`
2. **Generate `.env`** — `make setup`  (creates service principal, reads Terraform outputs)
3. **Deploy control plane** — `make deploy`  (rsync + docker compose up on the VM)

See [`INFRA.md`](INFRA.md) for current deployment IPs and endpoints.

## Workflow

1. Register a model: `make model-register NAME=llama3 TAG=llama3:8b SIZE=Standard_NC4as_T4_v3`
2. Provision a Spot VM: `make model-provision MODEL_ID=<id>`
3. Watch status: `make model-status MODEL_ID=<id>`  (`pending → provisioning → downloading → running`)
4. Send requests to `http://<CONTROL_PLANE_IP>:8000/v1/chat/completions` (OpenAI-compatible)

On Spot eviction the VM POSTs to `/api/vms/{vm_name}/evicted` and the control plane automatically starts a replacement.

## API endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/models` | GET, POST | List / register models |
| `/api/models/{id}/provision` | POST | Trigger Spot VM provisioning |
| `/api/models/{id}/instances` | GET | List VM instances |
| `/api/vms/{vm_name}/ready` | POST | Called by cloud-init when model is ready |
| `/api/vms/{vm_name}/evicted` | POST | Called on Spot eviction |

## Local development

```bash
temporal server start-dev          # terminal 1
uvicorn main:app --reload --port 8000  # terminal 2
python worker.py                   # terminal 3
```

## Tech stack

Python 3.11 · FastAPI · Temporal.io · Azure SDK · Azure Cosmos DB (serverless) · Ollama · Docker Compose · Terraform
