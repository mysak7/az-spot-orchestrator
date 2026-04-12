# GitHub Actions — CI / CD

## Overview

Two workflows guard every push. CI runs on all branches; CD runs only on `master`.

```
push to any branch  →  CI  (lint + typecheck + docker build)
push to master      →  CI  +  CD  (push image → deploy to control-plane VM)
```

---

## CI (`ci.yml`)

Triggered on every push to every branch.

| Job | Tool | What it checks |
|-----|------|----------------|
| **Lint** | `ruff check` + `ruff format --check` | Code style and formatting |
| **Type check** | `mypy` | Static type correctness across the whole codebase (Python 3.11, ignores `.venv`, `terraform`, test files) |
| **Docker build** | `docker/build-push-action` | The `Dockerfile` produces a valid image (push: false — build only, nothing is published) |

All three jobs run in parallel. If any fail the branch is considered broken.

---

## CD (`cd.yml`)

Triggered only on pushes to `master`. Requires CI to have passed (implicitly — same commit).

### Job 1 — Build & push image

Builds the Docker image and pushes two tags to **GitHub Container Registry (GHCR)**:

| Tag | Example | Purpose |
|-----|---------|---------|
| `sha-<git sha>` | `sha-b19e0bc` | Immutable, points to an exact commit |
| `latest` | `latest` | Convenience pointer, always the newest master build |

Authentication uses the built-in `GITHUB_TOKEN` — no extra secrets needed.

### Job 2 — Deploy to control-plane VM

Runs after Job 1. Deploys the new image to the Azure VM that hosts the control plane.

**Key design decision — no SSH keys in GitHub.** Instead it uses **OIDC / Workload Identity Federation**:

1. GitHub issues a short-lived OIDC token (no secret stored anywhere).
2. Azure AD validates it against the federated credential defined in `terraform/github_oidc.tf` (subject locked to the `master` branch).
3. The workflow gets a temporary Azure token scoped to what it needs.

**Deploy steps:**

```
1. az login (OIDC)
2. base64-encode the current docker-compose.yml
3. az vm run-command → on the control-plane VM:
     - write the new docker-compose.yml
     - docker login ghcr.io
     - docker compose pull api worker
     - docker compose up -d --no-deps api worker   ← zero-downtime rolling restart
     - docker image prune -f                        ← clean up old layers
4. az logout
```

Only the `api` and `worker` containers are restarted. The Temporal server, PostgreSQL, and other infrastructure containers are left untouched unless their config changes.

---

## Required GitHub configuration

| Type | Name | Used by |
|------|------|---------|
| Variable | `AZURE_CLIENT_ID` | CD — OIDC login |
| Variable | `AZURE_TENANT_ID` | CD — OIDC login |
| Variable | `AZURE_SUBSCRIPTION_ID` | CD — OIDC login |
| Secret | `GITHUB_TOKEN` | Auto-provided by GitHub — GHCR push + docker login on VM |

The OIDC federated credential on the Azure side is provisioned by Terraform (`terraform/github_oidc.tf`).
