CONTROL_PLANE_IP ?= $(shell cd terraform && terraform output -raw control_plane_public_ip 2>/dev/null)
SSH_USER         ?= azureuser
API_URL          ?= http://$(CONTROL_PLANE_IP)

.PHONY: dev setup deploy oidc-setup model-register model-provision model-status help

## Start all control-plane services locally (Temporal + API + Worker)
dev:
	@chmod +x scripts/start-dev.sh
	@./scripts/start-dev.sh

## Wire GitHub Actions → Azure OIDC trust (run once after terraform init)
## Creates App Registration, federated credential, role assignments, and sets
## the 3 GitHub Actions variables automatically via the GitHub Terraform provider.
oidc-setup:
	@command -v gh >/dev/null 2>&1 || (echo "ERROR: gh CLI not installed"; exit 1)
	@echo "==> Authenticating GitHub provider via gh CLI..."
	@cd terraform && GITHUB_TOKEN=$$(gh auth token) terraform init -upgrade -input=false
	@cd terraform && GITHUB_TOKEN=$$(gh auth token) terraform apply \
	  -target=azuread_application.github_actions \
	  -target=azuread_service_principal.github_actions \
	  -target=azuread_application_federated_identity_credential.cd_master \
	  -target=azuread_application_federated_identity_credential.ci_pr \
	  -target=azurerm_role_assignment.github_actions_contributor \
	  -target=azurerm_role_assignment.github_actions_blob \
	  -target=github_actions_variable.azure_client_id \
	  -target=github_actions_variable.azure_tenant_id \
	  -target=github_actions_variable.azure_subscription_id \
	  -input=false -auto-approve
	@echo ""
	@echo "Done. GitHub Actions variables set:"
	@cd terraform && terraform output github_actions_client_id
	@cd terraform && terraform output github_actions_tenant_id
	@cd terraform && terraform output github_actions_subscription_id

## Generate .env from Terraform outputs + Azure CLI
setup:
	@chmod +x scripts/setup-env.sh
	@./scripts/setup-env.sh

## Copy repo + .env to the control-plane VM and start docker compose
deploy:
	@echo "==> Syncing repo to $(CONTROL_PLANE_IP)..."
	rsync -az --exclude='.git' --exclude='terraform/.terraform' \
	  . $(SSH_USER)@$(CONTROL_PLANE_IP):~/az-spot-orchestrator/
	@echo "==> Starting docker compose..."
	ssh $(SSH_USER)@$(CONTROL_PLANE_IP) \
	  "cd ~/az-spot-orchestrator && docker compose pull && docker compose up -d"
	@echo ""
	@echo "Control plane: $(API_URL)"
	@echo "Temporal UI:   http://$(CONTROL_PLANE_IP):8080"

## Register a model. Usage: make model-register NAME=llama3 TAG=llama3:8b SIZE=Standard_NC4as_T4_v3
model-register:
	@test -n "$(NAME)" || (echo "Usage: make model-register NAME=llama3 TAG=llama3:8b SIZE=Standard_NC4as_T4_v3"; exit 1)
	curl -sf -X POST $(API_URL)/api/models \
	  -H 'Content-Type: application/json' \
	  -d '{"name":"$(NAME)","model_identifier":"$(or $(TAG),$(NAME):latest)","vm_size":"$(or $(SIZE),Standard_NC4as_T4_v3)","size_mb":0}' \
	  | python3 -m json.tool

## Provision a Spot VM for a model. Usage: make model-provision MODEL_ID=<id>
model-provision:
	@test -n "$(MODEL_ID)" || (echo "Usage: make model-provision MODEL_ID=<id>"; exit 1)
	curl -sf -X POST $(API_URL)/api/models/$(MODEL_ID)/provision \
	  -H 'Content-Type: application/json' \
	  -d '{}' \
	  | python3 -m json.tool

## Poll instance status. Usage: make model-status MODEL_ID=<id>
model-status:
	@test -n "$(MODEL_ID)" || (echo "Usage: make model-status MODEL_ID=<id>"; exit 1)
	@watch -n 3 "curl -sf $(API_URL)/api/models/$(MODEL_ID)/instances | python3 -m json.tool"

help:
	@grep -E '^##' Makefile | sed 's/## //'
