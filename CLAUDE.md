# Project Context: Dynamic Spot VM LLM Orchestrator
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

## Current Development Focus
We are currently focusing on:
1. Designing the API schema for the Image Selector (handling model registrations and test URLs).
2. Creating the Temporal workflow for fetching the cheapest Azure region and provisioning a VM.
3. Implementing the `cloud-init` script to mount the Temp disk (`/mnt/resource`) and download the model upon VM boot.
