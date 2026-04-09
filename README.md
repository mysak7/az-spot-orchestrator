# Azure Spot LLM Orchestrator

A control plane that dynamically provisions Azure Spot VMs running LLMs, routes inference traffic to the cheapest available VM, and automatically re-provisions on eviction.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Control Plane VM                    │
│  (stable, non-Spot — Standard_D2s_v3, swedencentral) │
│                                                      │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ FastAPI  │  │   Temporal   │  │   Cosmos DB   │  │
│  │ :8000    │  │   :7233      │  │  (serverless) │  │
│  │          │  │   UI: :8080  │  │               │  │
│  └────┬─────┘  └──────┬───────┘  └───────────────┘  │
│       │               │                              │
└───────┼───────────────┼──────────────────────────────┘
        │               │ ProvisionVMWorkflow
        │ proxy         ▼
        │      ┌─────────────────┐
        └─────►│  Spot VM        │
               │  Ollama :11434  │
               │  /mnt/resource  │  ← free temp disk for model weights
               └─────────────────┘
```

**Components:**

| Component | Tech | Purpose |
|---|---|---|
| API Gateway | FastAPI | Routes inference requests to active Spot VM; exposes model/VM management API |
| Workflow Engine | Temporal.io | Async orchestration of VM provisioning and eviction recovery |
| State Store | Azure Cosmos DB (serverless) | Tracks registered models and live VM instances |
| Inference Runtime | Ollama on Spot VM | Serves LLM requests; installed via cloud-init on boot |

## How it works

1. You register an LLM model (name + Ollama tag + VM size).
2. You trigger provisioning — Temporal finds the cheapest Azure region, spins up a Spot VM, and cloud-init installs Ollama and pulls the model.
3. Once the model is downloaded, the VM calls back the control plane (`/api/vms/{vm_name}/ready`) and starts serving traffic.
4. On eviction, the VM calls `/api/vms/{vm_name}/evicted` — the control plane immediately starts provisioning a replacement in a new region.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.3
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) — logged in (`az login`)
- SSH key at `~/.ssh/id_rsa.pub` or `~/.ssh/id_ed25519.pub`
- Docker + rsync on your local machine

## Deployment

### 1. Provision the control-plane infrastructure

```bash
cd terraform
terraform init
terraform apply
cd ..
```

### 2. Generate `.env` from Terraform outputs

```bash
make setup
```

This reads Terraform outputs, creates an Azure service principal with Contributor role, and writes `.env` automatically. If the SP already exists, set `AZURE_CLIENT_SECRET` manually.

### 3. Deploy the control plane to the Azure VM

```bash
make deploy
```

Rsyncs the repo, pulls Docker images, and starts `docker compose up -d` on the VM.

- API: `http://<VM_IP>:8000`
- Temporal UI: `http://<VM_IP>:8080`

## Deploying an LLM model

### Register the model

```bash
make model-register NAME=llama3 TAG=llama3:8b SIZE=Standard_NC4as_T4_v3
```

Save the `id` from the response.

### Provision a Spot VM

```bash
make model-provision MODEL_ID=<id>
```

### Watch status

```bash
make model-status MODEL_ID=<id>
```

Status progression: `pending` → `provisioning` → `downloading` → `running`

### Send inference requests

Once status is `running`, the proxy forwards OpenAI-compatible requests to the active Spot VM:

```bash
curl http://<VM_IP>:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "llama3:8b", "messages": [{"role": "user", "content": "Hello"}]}'
```

## VM Lifecycle API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/models` | GET, POST | List / register models |
| `/api/models/{id}` | GET, DELETE | Get / remove a model |
| `/api/models/{id}/provision` | POST | Trigger Spot VM provisioning |
| `/api/models/{id}/instances` | GET | List VM instances for a model |
| `/api/vms/{vm_name}/ready` | POST | Called by cloud-init when model is ready |
| `/api/vms/{vm_name}/evicted` | POST | Called by Spot VM on eviction; triggers re-provisioning |

## Tech stack

- **Python 3.11+**, FastAPI, Uvicorn
- **Temporal Python SDK** (`temporalio`)
- **Azure SDK** (`azure-mgmt-compute`, `azure-identity`)
- **Azure Cosmos DB** (serverless, SQL API)
- **Ollama** (inference on Spot VMs)
- **Docker / docker-compose** (control plane deployment)
- **Terraform** (control plane infrastructure)

## Development

Run everything locally (no Azure VM needed):

```bash
# Start Temporal dev server
temporal server start-dev

# In another terminal — start the API
uvicorn main:app --reload --port 8000

# In another terminal — start the worker
python worker.py
```
