output "control_plane_public_ip" {
  description = "Public IP of the control-plane VM — set as CONTROL_PLANE_URL in .env"
  value       = azurerm_public_ip.main.ip_address
}

output "cosmos_endpoint" {
  description = "Cosmos DB endpoint — auto-set as COSMOS_ENDPOINT Actions variable by terraform apply"
  value       = azurerm_cosmosdb_account.main.endpoint
}

output "resource_group_name" {
  description = "Resource group for Spot VM provisioning — set as AZURE_RESOURCE_GROUP in .env"
  value       = azurerm_resource_group.main.name
}

output "storage_account_name" {
  description = "Storage account for model cache — set as AZURE_STORAGE_ACCOUNT_NAME in .env"
  value       = azurerm_storage_account.model_cache.name
}

output "github_actions_client_id" {
  description = "AZURE_CLIENT_ID — set automatically in GitHub Actions variables by make oidc-setup"
  value       = azuread_application.github_actions.client_id
}

output "github_actions_tenant_id" {
  description = "AZURE_TENANT_ID — set automatically in GitHub Actions variables by make oidc-setup"
  value       = data.azurerm_client_config.current.tenant_id
}

output "github_actions_subscription_id" {
  description = "AZURE_SUBSCRIPTION_ID — set automatically in GitHub Actions variables by make oidc-setup"
  value       = data.azurerm_subscription.current.subscription_id
}
