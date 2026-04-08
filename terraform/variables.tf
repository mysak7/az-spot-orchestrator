variable "subscription_id" {
  type        = string
  description = "Azure subscription ID"
}

variable "resource_group_name" {
  type        = string
  default     = "az-spot-orchestrator-rg"
  description = "Name of the resource group for all control-plane resources"
}

variable "location" {
  type        = string
  default     = "eastus"
  description = "Azure region for the control-plane resources"
}

variable "environment" {
  type        = string
  default     = "dev"
  description = "Environment tag applied to all resources"
}

variable "postgres_server_name" {
  type        = string
  description = "Globally unique name for the PostgreSQL Flexible Server"
}

variable "postgres_admin_user" {
  type        = string
  default     = "psqladmin"
  description = "PostgreSQL administrator username"
}

variable "postgres_admin_password" {
  type        = string
  sensitive   = true
  description = "PostgreSQL administrator password (min 8 chars, mixed case + digit + special)"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key content for the control-plane VM admin user"
}
