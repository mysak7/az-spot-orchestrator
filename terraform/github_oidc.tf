# GitHub Actions → Azure trust via OIDC / Workload Identity Federation.
#
# No client secrets are created or stored anywhere. GitHub's OIDC provider
# issues a short-lived JWT per workflow run; Azure AD validates the token's
# subject claim to confirm it came from this repo + branch.
#
# Run once with:
#   make oidc-setup
#
# That target sets GITHUB_TOKEN from `gh auth token` automatically and runs
# terraform apply. After it completes the 3 Actions variables are set in GitHub
# and the CD workflow is ready — nothing to paste anywhere.

# ── GitHub provider (reads GITHUB_TOKEN from env, set by make oidc-setup) ─────
provider "github" {
  owner = "mysak7"
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
# One subject = one allowed caller. Azure AD rejects any token whose subject
# does not exactly match — prevents other repos or branches from impersonating.

# Master branch CD deploys
resource "azuread_application_federated_identity_credential" "cd_master" {
  application_id = azuread_application.github_actions.id
  display_name   = "github-cd-master"
  description    = "CD workflow on the master branch"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:mysak7/az-spot-orchestrator:ref:refs/heads/master"
}

# Pull requests (scope for future integration tests that need Azure)
resource "azuread_application_federated_identity_credential" "ci_pr" {
  application_id = azuread_application.github_actions.id
  display_name   = "github-ci-pr"
  description    = "CI workflow on pull requests"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:mysak7/az-spot-orchestrator:pull_request"
}

# ── Role assignments (least privilege, scoped to RG not subscription) ──────────
resource "azurerm_role_assignment" "github_actions_contributor" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.github_actions.object_id
}

resource "azurerm_role_assignment" "github_actions_blob" {
  scope                = azurerm_storage_account.model_cache.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azuread_service_principal.github_actions.object_id
}

# ── GitHub Actions variables (set automatically, nothing to paste) ─────────────
# These are not secrets — client_id / tenant_id / subscription_id are
# identifiers, not credentials. Stored as Variables, not Secrets.

resource "github_actions_variable" "azure_client_id" {
  repository    = "az-spot-orchestrator"
  variable_name = "AZURE_CLIENT_ID"
  value         = azuread_application.github_actions.client_id
}

resource "github_actions_variable" "azure_tenant_id" {
  repository    = "az-spot-orchestrator"
  variable_name = "AZURE_TENANT_ID"
  value         = data.azurerm_client_config.current.tenant_id
}

resource "github_actions_variable" "azure_subscription_id" {
  repository    = "az-spot-orchestrator"
  variable_name = "AZURE_SUBSCRIPTION_ID"
  value         = data.azurerm_subscription.current.subscription_id
}

resource "github_actions_variable" "azure_storage_account_name" {
  repository    = "az-spot-orchestrator"
  variable_name = "AZURE_STORAGE_ACCOUNT_NAME"
  value         = azurerm_storage_account.model_cache.name
}

resource "github_actions_variable" "control_plane_url" {
  repository    = "az-spot-orchestrator"
  variable_name = "CONTROL_PLANE_URL"
  value         = "http://${azurerm_public_ip.main.ip_address}"
}

resource "github_actions_variable" "cosmos_endpoint" {
  repository    = "az-spot-orchestrator"
  variable_name = "COSMOS_ENDPOINT"
  value         = azurerm_cosmosdb_account.main.endpoint
}

resource "github_actions_variable" "azure_resource_group" {
  repository    = "az-spot-orchestrator"
  variable_name = "AZURE_RESOURCE_GROUP"
  value         = azurerm_resource_group.main.name
}

# Public key is not sensitive — safe to store as a plain variable.
resource "github_actions_variable" "azure_ssh_public_key" {
  repository    = "az-spot-orchestrator"
  variable_name = "AZURE_SSH_PUBLIC_KEY"
  value         = local.ssh_public_key
}
