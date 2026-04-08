output "control_plane_public_ip" {
  description = "Public IP of the control-plane VM — set as CONTROL_PLANE_URL in .env"
  value       = azurerm_public_ip.control_plane.ip_address
}

output "postgres_fqdn" {
  description = "PostgreSQL Flexible Server FQDN for DATABASE_URL"
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "resource_group_name" {
  description = "Resource group that Temporal activities should target for Spot VM creation"
  value       = azurerm_resource_group.main.name
}

output "database_url" {
  description = "Full async DATABASE_URL for the application .env"
  sensitive   = true
  value = format(
    "postgresql+asyncpg://%s:%s@%s/az_spot_orchestrator",
    var.postgres_admin_user,
    var.postgres_admin_password,
    azurerm_postgresql_flexible_server.main.fqdn,
  )
}
