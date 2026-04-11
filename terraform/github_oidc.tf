# GitHub Actions → Azure trust via OIDC / Workload Identity Federation.
#
# No client secrets are created or stored. GitHub's OIDC provider issues
# short-lived tokens per workflow run; Azure AD validates the token's
# subject claim to confirm it originated from this repo + branch.
#
# Apply once:
#   terraform apply -target=azuread_application.github_actions \
#                   -target=azuread_service_principal.github_actions \
#                   -target=azuread_application_federated_identity_credential.cd_master \
#                   -target=azurerm_role_assignment.github_actions_contributor
#
# Then copy the three outputs into GitHub → Settings → Secrets and variables:
#   AZURE_CLIENT_ID      (Variable, not sensitive)
#   AZURE_TENANT_ID      (Variable, not sensitive)
#   AZURE_SUBSCRIPTION_ID (Variable, not sensitive)

terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.0"
    }
  }
}

data "azurerm_subscription" "current" {}
data "azurerm_client_config" "current" {}

# ── App Registration ───────────────────────────────────────────────────────────
resource "azuread_application" "github_actions" {
  display_name = "az-spot-orchestrator-github-actions"
}

resource "azuread_service_principal" "github_actions" {
  client_id = azuread_application.github_actions.client_id
}

# ── Federated credentials ──────────────────────────────────────────────────────
# One credential per "subject" (branch/PR/tag/environment). Only tokens whose
# subject exactly matches are accepted — no blanket repo-wide trust.

# Master branch pushes (CD deploys)
resource "azuread_application_federated_identity_credential" "cd_master" {
  application_id = azuread_application.github_actions.id
  display_name   = "github-cd-master"
  description    = "CD workflow on the master branch"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:mysak7/az-spot-orchestrator:ref:refs/heads/master"
}

# Pull requests (CI jobs that need Azure — e.g. integration tests)
resource "azuread_application_federated_identity_credential" "ci_pr" {
  application_id = azuread_application.github_actions.id
  display_name   = "github-ci-pr"
  description    = "CI workflow on pull requests"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:mysak7/az-spot-orchestrator:pull_request"
}

# ── Role assignments ───────────────────────────────────────────────────────────
# Principle of least privilege:
#
#  Contributor on the resource group — needed to provision Spot VMs, NICs, PIPs
#  and run VM run-commands for deployments. Scoped to the RG, not the subscription.
#
resource "azurerm_role_assignment" "github_actions_contributor" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.github_actions.object_id
}

# Storage Blob Data Contributor — needed to read/write the model cache container.
resource "azurerm_role_assignment" "github_actions_blob" {
  scope                = azurerm_storage_account.model_cache.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azuread_service_principal.github_actions.object_id
}

# ── Outputs (paste into GitHub → Settings → Secrets and variables) ─────────────
output "github_actions_client_id" {
  description = "Paste as AZURE_CLIENT_ID in GitHub repository variables (not a secret)"
  value       = azuread_application.github_actions.client_id
}

output "github_actions_tenant_id" {
  description = "Paste as AZURE_TENANT_ID in GitHub repository variables (not a secret)"
  value       = data.azurerm_client_config.current.tenant_id
}

output "github_actions_subscription_id" {
  description = "Paste as AZURE_SUBSCRIPTION_ID in GitHub repository variables (not a secret)"
  value       = data.azurerm_subscription.current.subscription_id
}
