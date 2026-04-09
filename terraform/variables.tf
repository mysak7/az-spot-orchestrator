variable "location" {
  type        = string
  default     = "swedencentral"
  description = "Azure region for control-plane resources"
}

variable "environment" {
  type        = string
  default     = "dev"
  description = "Environment tag applied to all resources"
}

variable "ssh_public_key" {
  type        = string
  default     = ""
  description = "SSH public key for the control-plane VM. Defaults to ~/.ssh/id_rsa.pub when empty."
}
