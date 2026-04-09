CONTROL_PLANE_IP ?= $(shell cd terraform && terraform output -raw control_plane_public_ip 2>/dev/null)
SSH_USER         ?= azureuser
API_URL          ?= http://$(CONTROL_PLANE_IP):8000

.PHONY: setup deploy model-register model-provision model-status help

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
