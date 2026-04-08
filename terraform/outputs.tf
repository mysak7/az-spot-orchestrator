output "control_plane_public_ip" {
  description = "Public IP of the control-plane VM — set as CONTROL_PLANE_URL in .env"
  value       = azurerm_public_ip.control_plane.ip_address
}

output "cosmos_endpoint" {
  description = "Cosmos DB endpoint — set as COSMOS_ENDPOINT in .env"
  value       = azurerm_cosmosdb_account.main.endpoint
}

output "cosmos_primary_key" {
  description = "Cosmos DB primary key — set as COSMOS_KEY in .env"
  value       = azurerm_cosmosdb_account.main.primary_key
  sensitive   = true
}

output "resource_group_name" {
  description = "Resource group for Spot VM provisioning (AZURE_RESOURCE_GROUP)"
  value       = azurerm_resource_group.main.name
}
