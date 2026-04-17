# Project Specifications: Dynamic Spot VM LLM Orchestrator
This is a Control Plane application designed to dynamically provision, manage, and route traffic to Azure Spot VMs running Large Language Models (LLMs). The system ensures high availability and cost optimization by migrating workloads to the cheapest Azure regions upon Spot eviction.

## System Architecture
The application consists of four main components running on a stable (non-Spot) control-plane server:
1. **Orchestrator (Temporal.io + Python):** The core workflow engine that handles asynchronous VM provisioning, Azure API interactions, and eviction recovery without blocking.
2. **API Gateway / Entrypoint (FastAPI / Azure APIM):** The dynamic router that forwards user LLM requests to the currently active Spot VM based on the requested model path.
3. **Image Selector & State Store (PostgreSQL):** Tracks registered LLM models, their sizes (e.g., a 500MB test model), and their real-time location/IP address.
4. **Spot VMs (Data Plane):** Ephemeral Azure instances provisioned dynamically. They use free Azure Temp disks (`/mnt/resource`) to download and cache model weights upon boot via `cloud-init`.

## Tech Stack & Tooling
- **Backend/Workflows:** Python 3.11+, Temporal Python SDK (`temporalio`), `asyncio`
- **Cloud Provider:** Azure SDK for Python (`azure-mgmt-compute`, `azure-identity`)
- **API Server:** FastAPI, Uvicorn
- **Database:** PostgreSQL (accessed via SQLAlchemy or asyncpg)
- **Deployment:** Docker, `docker-compose` for the control plane.
- **Infrastructure (Control Plane only):** Terraform

## Coding Standards & Conventions
- **Asynchronous Code:** Use `async`/`await` for all FastAPI endpoints and Temporal activities. Do not block the event loop.
- **Temporal Best Practices:**
  - Keep Temporal Workflows deterministic (no network calls, random numbers, or direct DB access inside workflows).
  - Put all side-effects (Azure API calls, DB updates) into Temporal Activities.
- **Azure SDK Usage:** Prefer asynchronous Azure SDK clients where possible. Always implement robust error handling for Azure API rate limits.
- **Type Hinting:** Enforce strict Python type hints for all functions, inputs, and outputs.
- **Docstrings:** Briefly document the purpose of Temporal workflows and complex activities.

## Development Workflow Commands
- Start Temporal Dev Server: `temporal server start-dev`
- Run FastAPI server: `uvicorn main:app --reload --port 8000`
- Run Temporal Worker: `python worker.py`

## Original Development Plan
1. Designing the API schema for the Image Selector (handling model registrations and test URLs).
2. Creating the Temporal workflow for fetching the cheapest Azure region and provisioning a VM.
3. Implementing the `cloud-init` script to mount the Temp disk (`/mnt/resource`) and download the model upon VM boot.

---

## What Was Done Differently

### LLM Serving: Ollama instead of a generic image
Spot VMs run **Ollama** (port 11434, OpenAI-compatible API) rather than a custom LLM server. The proxy route is `ANY /proxy/{model_name}/{path:path}` → `http://{vm_ip}:11434/{path}`.

### Model Caching: Azure Blob Storage per region
The original spec had no caching plan. Implemented a full blob-based caching layer:
- VM cloud-init checks `GET /api/storage/cache/source?model_identifier=X&region=$VM_REGION`
- If a blob exists in the VM's region → downloads via SAS URL + tar extract
- Otherwise → `ollama pull` (no auto-upload from VM)
- Two Temporal workflows manage blobs: `SeedBlobWorkflow` (pull from Ollama + upload) and `CopyBlobWorkflow` (server-side Azure cross-region copy)

### State Store: Cosmos DB added alongside PostgreSQL
Cosmos DB (key-based auth via `COSMOS_KEY`) tracks blob cache entries per region/model. The original spec only mentioned PostgreSQL. DefaultAzureCredential is not used for Cosmos because the service principal lacks RBAC roles.

### Spot Pricing: Azure Retail Prices public API
Spot prices are fetched from the Azure Retail Prices API (no auth required). The `priceType eq 'Consumption'` filter is used and Spot items are post-filtered by `"Spot"` in `skuName` — `priceType eq 'Spot'` does not exist in the API.

### VM Sizing: Standard_D2as_v4 (AMD, ~$0.02/hr)
B-series VMs do not support Spot. F2s_v2 and A2_v2 are over-subscribed. Reliable Spot availability uses `Standard_D2as_v4` (AMD, 8 GB RAM). OS disk uses `StandardSSD_LRS` (not `Premium_LRS`) for broadest VM compatibility.

### Two Deployment Environments
- **Production**: `az-spot-orchestrator-vm` at `135.225.121.221` (swedencentral), docker-compose on port 80
- **Local dev**: `78.80.157.131` — inbound port 80 blocked by ISP/firewall, so `CONTROL_PLANE_URL` must always point to production

### NIC Cleanup Delay
Azure holds NICs for ~180 s after a failed Spot VM creation. The delete activity catches `NicReservedForAnotherVm`, sleeps 180 s, and retries.

### Monitoring: ELK Stack
Added Elasticsearch + Logstash + Kibana + Filebeat for log aggregation and observability — not part of the original spec.

### cloud-init `HOME` Requirement
`ollama pull` inside cloud-init requires `HOME=/root OLLAMA_HOST=http://localhost:11434`; without `HOME` it panics. The `/mnt/resource/models` directory requires `chown -R ollama:ollama` after Ollama install so the service can write model weights.
