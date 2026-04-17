# Architecture Manual — Azure Spot LLM Orchestrator

A guide for engineers who need to understand, extend, or operate this platform.

---

## What the System Does

The orchestrator lets clients send OpenAI-compatible LLM inference requests to a stable endpoint while the actual model inference runs on cheap Azure Spot VMs. The control plane handles VM provisioning, region failover, eviction recovery, and model-weight caching so that clients never need to know which VM (or which Azure region) is serving them.

---

## High-Level Topology

```
Client (OpenAI-compatible request)
        │
        ▼
Control-plane VM  ─── stable public IP (swedencentral)
  ├─ FastAPI :8000       API + reverse-proxy
  ├─ Temporal :7233      workflow orchestration
  ├─ Cosmos DB           state store (models, VM instances, cache entries)
  ├─ Azure Blob Storage  model-weight cache per region
  ├─ Azure Files NFS     fast model-weight mount per region (optional)
  └─ ELK stack           log aggregation (:5601 Kibana)
        │
        │  provision / monitor / re-provision
        ▼
Spot VMs  (ephemeral, cheapest Azure region)
  └─ Ollama :11434       OpenAI-compatible LLM API
     weights live on /mnt/resource (free Azure temp disk)
```

All services on the control plane run in Docker Compose. The control plane VM is stable (non-Spot). Spot VMs are fully ephemeral — they can be deleted and replaced at any time.

---

## Component Breakdown

### FastAPI (`main.py`, `api/`)

Entry point for all traffic. Responsibilities:

- **Reverse-proxy** (`api/routes/proxy.py`) — forwards `POST /proxy/{model_name}/…` to the active Spot VM's Ollama.
- **Model management** (`api/routes/models.py`) — CRUD for `LLMModel` records; triggers `ProvisionVMWorkflow`.
- **VM lifecycle callbacks** — Spot VMs call `POST /api/vms/{vm_name}/evicted` and `POST /api/vms/{vm_name}/ready`.
- **Storage / cache management** (`api/routes/storage.py`) — start `SeedBlobWorkflow`, `CopyBlobWorkflow`, `SeedFilesWorkflow`; query cache state.
- **Spot inventory** (`api/routes/inventory.py`) — query Azure Retail Prices API for current Spot availability.
- **Static dashboard** — served from `/static/`, redirected from `/`.

At startup (`lifespan`):
1. Connect to Cosmos DB and seed default models.
2. Connect to Temporal.
3. Background task: warm the Spot inventory price cache.
4. Background task: `_keep_alive_watchdog` (runs every 60 s).

### Temporal Worker (`worker.py`)

Runs all workflows and activities. Registered task queue: configured via `TEMPORAL_TASK_QUEUE` (default `az-spot-orchestrator`).

**Rule**: Workflows are deterministic — no I/O, no DB calls, no randomness. All side-effects live in Activities.

### Cosmos DB (`db/cosmos.py`, `db/models.py`)

Key-auth (not RBAC). Four containers:

| Container | Partition key | Document type |
|---|---|---|
| `models` | `/id` | `LLMModel` |
| `vm-instances` | `/id` (= `vm_name`) | `VMInstance` |
| `cache-entries` | `/id` (= `{model}-{region}`) | `ModelCacheEntry` |
| `files-shares` | `/id` (= `{model}-{region}`) | `FilesShareEntry` |
| `system-messages` | `/id` | `SystemMessage` |

Partition key equals document id everywhere — all reads are direct point-reads (cheap, O(1)).

---

## Domain Model

### `LLMModel`
Registered model definition. Created once, referenced by all VM instances.

```
id                  UUID — Cosmos document id + partition key
name                Human-readable routing name (used in proxy URL)
model_identifier    Ollama tag, e.g. "llama3:8b"
vm_size             Azure VM SKU, e.g. "Standard_NC4as_T4_v3"
keep_alive          If true, watchdog ensures a VM always exists for this model
size_mb             Approximate weight size (informational)
```

### `VMInstance`
Tracks one Spot VM's lifecycle. `id` = `vm_name`.

```
VMStatus lifecycle:
  pending → provisioning → downloading → running
                                       ↘ evicted → (new VMInstance)
                                       ↘ terminated
```

`model_name` is denormalised from `LLMModel.name` so the proxy can look up the VM without a join.

### `ModelCacheEntry`
One blob archive per `(model_identifier, region)`. Tracks upload state, size, and download stats.

### `FilesShareEntry`
One Azure Files NFS share per `(model_identifier, region)`. Faster than blob download for large models.

---

## Data Flows

### 1. Inference Request

```
POST /proxy/llama3/{path}
  │
  ├─ Query Cosmos: SELECT * FROM c WHERE model_name='llama3' AND status='running'
  │  → returns VMInstance with ip_address
  │
  ├─ 503 if no running instance
  │
  └─ HTTP forward → http://{vm_ip}:11434/{path}  (300 s timeout)
       → stream response back to client
```

Key file: `api/routes/proxy.py:22`

### 2. Provisioning Flow

```
POST /api/models/{id}/provision
  │
  ├─ Create VMInstance(status=pending) in Cosmos
  └─ Start ProvisionVMWorkflow in Temporal
        │
        ├─ Activity: get_cheapest_region
        │    Query Azure Retail Prices API → sort candidate regions by Spot price
        │
        ├─ For each region (cheapest first):
        │    ├─ Activity: check_files_share_ready
        │    │    → if NFS share exists: use generate_cloud_init_with_files_mount
        │    │    → else: use generate_cloud_init (blob/Ollama path)
        │    │
        │    ├─ Activity: provision_azure_vm
        │    │    Create: Public IP → NIC → VM (with cloud-init)
        │    │    On SkuNotAvailable: fire-and-forget cleanup → try next region
        │    │
        │    └─ break on success
        │
        ├─ Activity: update_vm_status(provisioning, region, ip)
        ├─ Activity: update_vm_status(downloading)
        ├─ Activity: wait_for_model_ready  ← polls Ollama GET /api/tags, 45 min timeout
        └─ Activity: update_vm_status(running)
```

Key files: `temporal/workflows/vm_provisioning.py:68`, `api/routes/models.py:106`

### 3. Eviction Recovery

```
Spot VM  →  IMDS poll /scheduledevents every 15 s
              On Preempt event:
                POST /api/vms/{vm_name}/evicted
                  │
                  ├─ Mark VMInstance(evicted) in Cosmos
                  ├─ Start DeleteVMWorkflow (fire-and-forget)
                  └─ Start ProvisionVMWorkflow for a new VM (new vm_name, same model)

Keep-alive watchdog (60 s interval):
  For each model with keep_alive=True:
    If no active (non-stale, non-terminal) VMInstance exists:
      → Start ProvisionVMWorkflow
  Stale = stuck in pre-running state for > 20 min → mark terminated
```

Key file: `api/routes/models.py:236`, `main.py:35`

### 4. VM Startup (cloud-init)

```
New Spot VM boots
  │
  ├─ Install Ollama
  ├─ GET /api/storage/cache/source?model_identifier=X&region=Y
  │    Returns one of:
  │    ├─ type=files: NFS mount path   → mount + ollama pull (fast, ~2 min)
  │    ├─ type=blob: SAS URL           → wget tar.gz → extract → register
  │    └─ type=none                    → ollama pull from registry (~15–45 min)
  │
  ├─ Start eviction monitor (polls IMDS every 15 s)
  └─ POST /api/vms/{vm_name}/ready   ← control plane marks VMInstance running
```

Key file: `cloud_init/vm_setup.py`

---

## Model Weight Cache

Caching avoids re-downloading weights (often gigabytes) on every new VM.

### Blob Cache

Tar + lz4-compress model weights → upload to regional Azure Blob Storage.

```
POST /api/storage/cache/seed   → SeedBlobWorkflow
  Pulls model via Ollama on control plane → archives → uploads to blob

POST /api/storage/cache/copy   → CopyBlobWorkflow
  Server-side Azure cross-region copy (no data leaves Azure backbone)
```

Cache source resolution (`GET /api/storage/cache/source`):
1. Same-region blob → SAS download URL
2. Nearest-region blob → SAS download URL
3. No cache → `type=none` → VM does `ollama pull`

### Azure Files NFS Cache (faster)

Blob contents are copied into an Azure Files NFS share in the target region. VMs mount the share directly.

```
POST /api/storage/files/seed   → SeedFilesWorkflow
  Requires blob to exist first. Copies blob → NFS share.
  Boot-to-ready: ~2 min vs 15–45 min for blob/pull.

POST /api/storage/files/create-share   → CreateFilesShareWorkflow
  Pre-provision NFS infrastructure (FileStorage account + VNet endpoint + share)
  without copying data. Idempotent.
```

Priority order during provisioning:
1. NFS Files share in candidate region → fastest
2. Blob in same/nearest region → fast
3. `ollama pull` → slowest

---

## Temporal Workflows Reference

| Workflow | Trigger | What it does |
|---|---|---|
| `ProvisionVMWorkflow` | `POST /models/{id}/provision`, eviction, watchdog | Full VM provision + model ready |
| `DeleteVMWorkflow` | Eviction, manual `DELETE /vms/{name}` | Delete VM + NIC + Public IP from Azure |
| `LaunchBareVMWorkflow` | Admin endpoint | Provision SSH-only VM (no Ollama, no model) |
| `SeedBlobWorkflow` | `POST /storage/cache/seed` | Pull model → tar+lz4 → upload blob |
| `CopyBlobWorkflow` | `POST /storage/cache/copy` | Server-side Azure blob copy to new region |
| `SeedFilesWorkflow` | `POST /storage/files/seed` | Copy blob → Azure Files NFS share |
| `CreateFilesShareWorkflow` | `POST /storage/files/create-share` | Provision NFS infra only |

Key files:
- `temporal/workflows/vm_provisioning.py`
- `temporal/workflows/seed_blob.py`
- `temporal/workflows/blob_copy.py`
- `temporal/workflows/seed_files.py`
- `temporal/workflows/create_files_share.py`
- `temporal/activities/azure.py`
- `temporal/activities/database.py`
- `temporal/activities/blob.py`
- `temporal/activities/files.py`

---

## API Surface

### Models
| Method | Path | Description |
|---|---|---|
| `POST` | `/api/models` | Register a new LLM model |
| `GET` | `/api/models` | List all registered models |
| `GET` | `/api/models/{id}` | Get model by id |
| `PATCH` | `/api/models/{id}` | Update model fields (e.g. `keep_alive`) |
| `DELETE` | `/api/models/{id}` | Deregister model |
| `POST` | `/api/models/{id}/provision` | Provision a Spot VM for this model |
| `GET` | `/api/models/{id}/instances` | List VM instances for model |

### VMs
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/instances` | List all VM instances |
| `DELETE` | `/api/vms/{vm_name}` | Terminate a VM |
| `POST` | `/api/vms/{vm_name}/evicted` | Eviction callback (called by Spot VM) |
| `POST` | `/api/vms/{vm_name}/ready` | Ready callback (called by cloud-init) |

### Storage / Cache
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/storage/cache` | List all cache entries |
| `GET` | `/api/storage/cache/source` | Best source for `?model_identifier=X&region=Y` |
| `POST` | `/api/storage/cache/seed` | Seed initial blob for model+region |
| `POST` | `/api/storage/cache/copy` | Copy blob to another region |
| `DELETE` | `/api/storage/cache/entry` | Delete one cache entry |
| `DELETE` | `/api/storage/cache/model` | Delete all entries for a model |
| `GET` | `/api/storage/files` | List Azure Files NFS shares |
| `POST` | `/api/storage/files/seed` | Seed model into NFS share |
| `POST` | `/api/storage/files/create-share` | Pre-provision NFS infra |
| `GET` | `/api/storage/regions` | Candidate regions list |

### Proxy
| Method | Path | Description |
|---|---|---|
| `*` | `/proxy/{model_name}/{path:path}` | Forward to active Spot VM Ollama |

---

## Keep-Alive Watchdog

Models with `keep_alive=True` are automatically re-provisioned if their VM disappears.

The watchdog runs every 60 s (with a 15 s startup delay). Logic per keep-alive model:
1. Query all non-terminal VM instances.
2. Classify each as healthy (running) or stale (stuck in pre-running > 20 min).
3. Mark stale instances as `terminated`.
4. If no healthy instance remains → start `ProvisionVMWorkflow`.

To enable for a model: `PATCH /api/models/{id}` with `{"keep_alive": true}`.

---

## Environment Variables

Generated by `make setup` (reads Terraform outputs + Azure CLI):

| Variable | Purpose |
|---|---|
| `COSMOS_ENDPOINT` / `COSMOS_KEY` | Cosmos DB — key auth, not RBAC |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription |
| `AZURE_RESOURCE_GROUP` | Resource group for Spot VMs |
| `AZURE_STORAGE_ACCOUNT_NAME` | Blob cache storage account |
| `CONTROL_PLANE_URL` | Public IP embedded in cloud-init; must be production IP even during local dev |
| `CONTROL_PLANE_REGION` | Azure region of the control-plane VM |
| `TEMPORAL_HOST` | `temporal:7233` in Docker Compose; `localhost:7233` for local dev server |
| `TEMPORAL_TASK_QUEUE` | Task queue name (default `az-spot-orchestrator`) |
| `AZURE_CANDIDATE_REGIONS` | JSON array of regions; Spot VMs try cheapest-first |

---

## Infrastructure

Provisioned by Terraform (`terraform/`):
- Control-plane VM (stable, non-Spot)
- Cosmos DB account
- Azure Blob Storage accounts (one per candidate region)
- NSG rules (inbound 8000, 5601, 22)

CI/CD via GitHub Actions with OIDC (no stored credentials):
- CI (PRs): `ruff check`, `ruff format --check`, `mypy`, docker build
- CD (master): build + push to GHCR → `az vm run-command invoke` to deploy

---

## Observability

ELK stack runs on the control-plane VM alongside the application services.

```
Docker container stdout (JSON via structlog)
  → Filebeat (reads /var/lib/docker/containers/*/*.log)
  → Elasticsearch (index: az-spot-orchestrator-YYYY.MM.DD)
  → Kibana :5601
```

All application logs are structured JSON. Fields searchable in Kibana:
- `event`, `level`, `timestamp`, `logger`
- `model_name`, `vm_name`, `region`, `ip_address`
- `duration_ms`, `status`, `error`

See `docs/elk-monitoring.md` for Kibana setup and useful queries.

---

## Known Constraints and Gotchas

**NIC reservation delay** — Azure holds NICs for ~180 s after a failed Spot VM creation. The `delete_azure_vm` activity catches `NicReservedForAnotherVm`, sleeps 180 s, and retries.

**Spot pricing API** — `priceType eq 'Spot'` does not exist. Use `priceType eq 'Consumption'` and post-filter by `"Spot"` in `skuName`.

**VM sizing** — B-series does not support Spot. F2s_v2 and A2_v2 are over-subscribed. Reliable choice for CPU: `Standard_D2as_v4` (AMD, 8 GB RAM, ~$0.02/hr).

**`CONTROL_PLANE_URL`** — Must always point to the production public IP, even when developing locally. It is embedded in cloud-init and the Spot VM calls back to it from the public internet.

**`ollama pull` in cloud-init** — Requires `HOME=/root OLLAMA_HOST=http://localhost:11434` in the environment. Without `HOME`, Ollama panics. The `/mnt/resource/models` directory requires `chown -R ollama:ollama` after Ollama install.

**Cosmos key auth** — The service principal used for OIDC/GitHub Actions lacks RBAC roles on Cosmos. `COSMOS_KEY` is used for all Cosmos access.

**One VM per model** — The proxy picks the single most-recently-created `running` instance for a model. Multiple parallel VMs per model are not supported by the proxy; only one is used.
