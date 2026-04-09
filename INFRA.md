# Infrastructure — Current Deployment

## Control Plane VM

| | |
|---|---|
| **Public IP** | `74.241.243.18` |
| **Size** | Standard_D2s_v3 |
| **Region** | swedencentral |
| **OS** | Ubuntu 22.04 LTS |
| **SSH user** | `azureuser` |
| **SSH** | `ssh azureuser@74.241.243.18` |

## Endpoints

| Service | URL |
|---|---|
| API | http://74.241.243.18:8000 |
| API docs | http://74.241.243.18:8000/docs |
| Temporal UI | http://74.241.243.18:8080 |

> Access restricted to `78.80.157.131` (NSG rules on ports 22, 8000, 8080).

## Azure Resources

| Resource | Name |
|---|---|
| Resource group | `az-spot-orchestrator-rg` |
| Cosmos DB account | `az-spot-orchestrator` |
| Cosmos DB endpoint | `https://az-spot-orchestrator.documents.azure.com:443/` |
| Terraform state storage | `azspotorchtfstate` / container `tfstate` |
| Terraform state RG | `az-spot-orchestrator-tfstate-rg` |

## Spot VM defaults

| | |
|---|---|
| Default size | `Standard_NC4as_T4_v3` |
| Admin user | `azureuser` |
| Inference port | `11434` (Ollama) |
| Model disk | `/mnt/resource` (free Azure temp disk) |

## Networking (NSG)

| Rule | Port | Source |
|---|---|---|
| allow-ssh | 22 | 78.80.157.131 |
| allow-api | 8000 | 78.80.157.131 |
| allow-temporal-ui | 8080 | 78.80.157.131 |

## Deployment commands

```bash
# Full redeploy
make deploy

# SSH into control plane
ssh azureuser@74.241.243.18

# Check running services
ssh azureuser@74.241.243.18 "docker compose ps"

# View logs
ssh azureuser@74.241.243.18 "docker compose logs -f"
```

## Environment variables

Secrets live in `.env` (gitignored). Re-generate from Terraform outputs:

```bash
make setup
```

Key variables:

```
CONTROL_PLANE_URL=http://74.241.243.18:8000
AZURE_RESOURCE_GROUP=az-spot-orchestrator-rg
COSMOS_ENDPOINT=https://az-spot-orchestrator.documents.azure.com:443/
TEMPORAL_HOST=localhost:7233
DEFAULT_VM_SIZE=Standard_NC4as_T4_v3
```
