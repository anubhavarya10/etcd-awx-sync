#!/bin/bash
# Deploy script for tf-manager
# Requires: TF_GITHUB_TOKEN (github.com PAT) and TFC_TOKEN (Terraform Cloud API token)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env file if it exists
if [ -f ../../../.env ]; then
    echo "Loading secrets from .env file..."
    export $(grep -v '^#' ../../../.env | xargs)
fi

# Check required variables
if [ -z "$TF_GITHUB_TOKEN" ]; then
    echo "ERROR: TF_GITHUB_TOKEN is not set (github.com PAT with repo scope)"
    exit 1
fi

if [ -z "$TFC_TOKEN" ]; then
    echo "ERROR: TFC_TOKEN is not set (Terraform Cloud API token)"
    exit 1
fi

if [ -z "$PASSWORD" ]; then
    echo "WARNING: PASSWORD is not set (SSH password for jump host - needed for OpenStack queries)"
fi

echo "Creating/updating Kubernetes resources..."

# Apply configmap
kubectl apply -f configmap.yaml

# Create secrets (delete first if exists)
kubectl delete secret tf-manager-secrets --ignore-not-found
kubectl create secret generic tf-manager-secrets \
    --from-literal=TF_GITHUB_TOKEN="$TF_GITHUB_TOKEN" \
    --from-literal=TFC_TOKEN="$TFC_TOKEN" \
    --from-literal=SSH_PASSWORD="${PASSWORD:-}"

# Apply deployment
kubectl apply -f deployment.yaml

echo ""
echo "Deployment complete! Checking pod status..."
kubectl get pods -l app=tf-manager

echo ""
echo "To view logs: kubectl logs -l app=tf-manager -f"
