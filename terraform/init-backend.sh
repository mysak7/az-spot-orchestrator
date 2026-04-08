#!/usr/bin/env bash
# Bootstrap script — run ONCE before `terraform init`.
# Creates the Azure Storage Account that will hold the Terraform state file.
# The storage account name must be globally unique; change STORAGE_ACCOUNT_NAME
# if "azspottfstate" is already taken.
set -euo pipefail

RESOURCE_GROUP="az-spot-tfstate-rg"
LOCATION="eastus"
STORAGE_ACCOUNT_NAME="azspottfstate"
CONTAINER_NAME="tfstate"

echo "==> Creating resource group: $RESOURCE_GROUP"
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output none

echo "==> Creating storage account: $STORAGE_ACCOUNT_NAME"
az storage account create \
  --name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --encryption-services blob \
  --min-tls-version TLS1_2 \
  --allow-blob-public-access false \
  --output none

echo "==> Enabling versioning (soft-delete protection for state files)"
az storage account blob-service-properties update \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --enable-versioning true \
  --output none

echo "==> Creating blob container: $CONTAINER_NAME"
az storage container create \
  --name "$CONTAINER_NAME" \
  --account-name "$STORAGE_ACCOUNT_NAME" \
  --auth-mode login \
  --output none

echo ""
echo "Backend ready. Now run:"
echo "  cd terraform && terraform init"
