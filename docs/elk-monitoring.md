# ELK Stack Monitoring

## Overview

The az-spot-orchestrator uses an ELK stack (Elasticsearch + Kibana + Filebeat) to collect, store, and visualize structured logs from all control-plane services.

## What Is Being Monitored

### Services
| Service | Description |
|---|---|
| `api` | FastAPI control-plane — HTTP requests, routing decisions, errors |
| `worker` | Temporal worker — activity execution, Azure API calls, VM provisioning |
| `temporal` | Temporal dev server — workflow scheduling and state transitions |
| `elasticsearch` | Log storage health |
| `kibana` | Dashboard access |
| `filebeat` | Log shipper health |

### Log Events Captured
- **HTTP requests** — method, path, status code, latency
- **Provisioning workflows** — VM creation, region selection, Spot eviction recovery
- **Azure SDK calls** — pricing queries, NIC/VM/disk operations, errors and retries
- **Model routing** — which model was requested, which Spot VM it was forwarded to
- **Errors and exceptions** — stack traces, failed activities, retries

## How It Works

```
Docker containers
      │  stdout JSON logs
      ▼
  Filebeat
  (reads /var/lib/docker/containers/*/*.log)
  (adds Docker metadata: container name, image, labels)
      │
      ▼
  Elasticsearch
  index: az-spot-orchestrator-YYYY.MM.DD
      │
      ▼
  Kibana
  (Data View, Discover, Dashboards, Alerts)
```

### Log Format

All application logs are emitted as structured JSON via `structlog`:

```json
{
  "event": "vm_provisioned",
  "level": "info",
  "timestamp": "2026-04-17T10:23:45Z",
  "logger": "worker",
  "region": "eastus",
  "vm_name": "spot-vm-eastus-abc123",
  "model": "llama3",
  "duration_ms": 42300
}
```

Filebeat decodes the `message` field as JSON and flattens it into the Elasticsearch document, so all structured fields (`region`, `vm_name`, `model`, etc.) are individually searchable.

### Key Configuration

| Component | Config file | Notes |
|---|---|---|
| Filebeat | `filebeat.yml` | Container log input, Docker metadata processor, ES output |
| structlog | `logging_config.py` | JSON renderer, ISO timestamps, stdlib log routing |
| Elasticsearch | `docker-compose.yml` | Single-node, security disabled, 512MB heap |
| Index | `az-spot-orchestrator-*` | Daily rolling index, ILM disabled |

## Accessing Kibana

Kibana is available at `http://<control-plane-ip>:5601`.

### First-Time Setup
1. Go to **Stack Management → Data Views**
2. Create a new data view with pattern `az-spot-orchestrator-*`
3. Set `@timestamp` as the time field
4. Navigate to **Discover** to query logs

## Useful Kibana Queries

```
# All errors
level: "error"

# Provisioning events only
event: "vm_provisioned" OR event: "vm_evicted"

# Logs from a specific region
region: "eastus"

# Slow Azure API calls (if duration is logged)
duration_ms > 10000
```
