#!/bin/bash
# Deploy script for slack-mcp-agent
# Reads secrets from environment variables or .env file

set -e

# Load .env file if it exists
if [ -f ../.env ]; then
    echo "Loading secrets from .env file..."
    export $(grep -v '^#' ../.env | xargs)
elif [ -f .env ]; then
    echo "Loading secrets from .env file..."
    export $(grep -v '^#' .env | xargs)
fi

# Verify required environment variables
REQUIRED_VARS="SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_CHANNEL_ID AWX_CLIENT_ID AWX_CLIENT_SECRET AWX_USERNAME AWX_PASSWORD"
for var in $REQUIRED_VARS; do
    if [ -z "${!var}" ]; then
        echo "ERROR: $var is not set"
        exit 1
    fi
done

echo "Creating/updating Kubernetes resources..."

# Apply configmap
kubectl apply -f configmap.yaml

# Create secret from environment variables (delete first if exists)
kubectl delete secret slack-mcp-agent-secrets --ignore-not-found

kubectl create secret generic slack-mcp-agent-secrets \
    --from-literal=SLACK_BOT_TOKEN="$SLACK_BOT_TOKEN" \
    --from-literal=SLACK_APP_TOKEN="$SLACK_APP_TOKEN" \
    --from-literal=SLACK_CHANNEL_ID="$SLACK_CHANNEL_ID" \
    --from-literal=AWX_CLIENT_ID="$AWX_CLIENT_ID" \
    --from-literal=AWX_CLIENT_SECRET="$AWX_CLIENT_SECRET" \
    --from-literal=AWX_USERNAME="$AWX_USERNAME" \
    --from-literal=AWX_PASSWORD="$AWX_PASSWORD"

# Apply deployment
kubectl apply -f deployment.yaml

echo ""
echo "Deployment complete! Checking pod status..."
kubectl get pods -l app=slack-mcp-agent

echo ""
echo "To view logs: kubectl logs -l app=slack-mcp-agent -f"
