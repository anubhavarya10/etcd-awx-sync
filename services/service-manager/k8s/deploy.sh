#!/bin/bash
# Deploy script for service-manager
# Requires: SSH private key file and AZURE_SUDO_PASSWORD

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env file if it exists
if [ -f ../../../.env ]; then
    echo "Loading secrets from .env file..."
    export $(grep -v '^#' ../../../.env | xargs)
fi

# Check required variables
if [ -z "$AZURE_SUDO_PASSWORD" ]; then
    echo "ERROR: AZURE_SUDO_PASSWORD is not set"
    exit 1
fi

if [ -z "$AWX_PASSWORD" ]; then
    echo "ERROR: AWX_PASSWORD is not set"
    exit 1
fi

if [ -z "$PASSWORD" ]; then
    echo "ERROR: PASSWORD (root SSH password) is not set"
    exit 1
fi

# SSH key file path
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_rsa}"
if [ ! -f "$SSH_KEY_FILE" ]; then
    echo "ERROR: SSH key file not found: $SSH_KEY_FILE"
    echo "Set SSH_KEY_FILE environment variable to the correct path"
    exit 1
fi

echo "Creating/updating Kubernetes resources..."

# Apply configmap
kubectl apply -f configmap.yaml

# Create SSH key secret (delete first if exists)
kubectl delete secret service-manager-ssh --ignore-not-found
kubectl create secret generic service-manager-ssh \
    --from-file=id_rsa="$SSH_KEY_FILE"

# Create secrets (delete first if exists)
kubectl delete secret service-manager-secrets --ignore-not-found
kubectl create secret generic service-manager-secrets \
    --from-literal=AZURE_SUDO_PASSWORD="$AZURE_SUDO_PASSWORD" \
    --from-literal=AWX_PASSWORD="$AWX_PASSWORD" \
    --from-literal=PASSWORD="$PASSWORD"

# Apply deployment
kubectl apply -f deployment.yaml

echo ""
echo "Deployment complete! Checking pod status..."
kubectl get pods -l app=service-manager

echo ""
echo "To view logs: kubectl logs -l app=service-manager -f"
