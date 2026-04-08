variable "subscription_id" {
  type        = string
  description = "Azure subscription ID"
}

variable "resource_group_name" {
  type        = string
  default     = "az-spot-orchestrator-rg"
  description = "Resource group for all control-plane resources"
}

variable "location" {
  type        = string
  default     = "eastus"
  description = "Azure region for control-plane resources"
}

variable "environment" {
  type        = string
  default     = "dev"
  description = "Environment tag applied to all resources"
}

variable "cosmos_account_name" {
  type        = string
  description = "Globally unique name for the Cosmos DB account (lowercase letters, digits, hyphens)"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key for the control-plane VM admin user"
}
