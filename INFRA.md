# Infrastructure — Current Deployment

## Control Plane VM

| | |
|---|---|
| **Public IP** | `135.225.121.221` |
| **Size** | Standard_D2s_v3 |
| **Region** | swedencentral |
| **OS** | Ubuntu 22.04 LTS |
| **SSH user** | `azureuser` |
| **SSH** | `ssh azureuser@135.225.121.221` |

## Endpoints

| Service | URL |
|---|---|
| Dashboard | http://135.225.121.221/ |
| API | http://135.225.121.221/api |
| API docs | http://135.225.121.221/docs |
| Temporal UI | http://135.225.121.221:8080 |

> Access restricted to your IP (NSG rules on ports 22, 80, 8080).

## Azure Resources

| Resource | Name |
|---|---|
| Resource group | `az-spot-orchestrator-rg` |
| Cosmos DB account | `az-spot-orchestrator` |
| Cosmos DB endpoint | `https://az-spot-orchestrator.documents.azure.com:443/` |
| Terraform state storage | `azspotorchtfstate` / container `tfstate` |
| Terraform state RG | `az-spot-orchestrator-tfstate-rg` |

## Cosmos DB containers (Terraform-managed)

| Container | Name |
|---|---|
| Model registry | `llm-models` |
| VM instances | `vm-instances` |
| Model cache | `model-cache` |

> Schema is owned by Terraform. The app uses these containers directly — it does not create them.

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
| allow-ssh | 22 | your IP |
| allow-api | 80 | your IP |
| allow-temporal-ui | 8080 | your IP |

## Post-terraform deploy steps

After `terraform apply`:

1. **Trigger CD** — push any commit to `master` (or re-run the CD workflow in GitHub Actions). This deploys the app and starts all containers including Temporal.

2. **OR deploy manually:**
   ```bash
   make deploy
   ```

3. **Check status:**
   ```bash
   ssh azureuser@135.225.121.221 "docker compose -f ~/az-spot-orchestrator/docker-compose.yml ps"
   ```

> Note: Terraform's cloud-init only installs Docker. The app is deployed separately via CD or `make deploy`.

## Deployment commands

```bash
# Full redeploy
make deploy

# SSH into control plane
ssh azureuser@135.225.121.221

# Check running services
ssh azureuser@135.225.121.221 "docker compose -f ~/az-spot-orchestrator/docker-compose.yml ps"

# View logs
ssh azureuser@135.225.121.221 "docker compose -f ~/az-spot-orchestrator/docker-compose.yml logs -f"
```

## Environment variables

Secrets live in `.env` (gitignored). Re-generate from Terraform outputs:

```bash
make setup
```

Key variables:

```
CONTROL_PLANE_URL=http://135.225.121.221
AZURE_RESOURCE_GROUP=az-spot-orchestrator-rg
COSMOS_ENDPOINT=https://az-spot-orchestrator.documents.azure.com:443/
TEMPORAL_HOST=temporal:7233
```
