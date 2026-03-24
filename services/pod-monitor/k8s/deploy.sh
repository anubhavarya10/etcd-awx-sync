#!/bin/bash
# Deploy script for pod-monitor
# Optional: SLACK_ALERT_WEBHOOK_URL for pod health alerts

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env file if it exists
if [ -f ../../../.env ]; then
    echo "Loading secrets from .env file..."
    export $(grep -v '^#' ../../../.env | xargs)
fi

echo "Creating/updating Kubernetes resources..."

# Apply configmap
kubectl apply -f configmap.yaml

# Create secrets (bot token enables Block Kit alerts with buttons)
WEBHOOK_URL="${SLACK_ALERT_WEBHOOK_URL:-}"
BOT_TOKEN="${SLACK_BOT_TOKEN:-}"

SECRET_ARGS=()
if [ -n "$WEBHOOK_URL" ]; then
    SECRET_ARGS+=(--from-literal=SLACK_ALERT_WEBHOOK_URL="$WEBHOOK_URL")
fi
if [ -n "$BOT_TOKEN" ]; then
    SECRET_ARGS+=(--from-literal=SLACK_BOT_TOKEN="$BOT_TOKEN")
fi

if [ ${#SECRET_ARGS[@]} -gt 0 ]; then
    kubectl delete secret pod-monitor-secrets --ignore-not-found
    kubectl create secret generic pod-monitor-secrets "${SECRET_ARGS[@]}"
    echo "Created pod-monitor-secrets"
else
    echo "WARNING: Neither SLACK_BOT_TOKEN nor SLACK_ALERT_WEBHOOK_URL set, alerting will be disabled"
fi

# Apply deployment (includes ServiceAccount, ClusterRole, ClusterRoleBinding, Deployment, Service)
kubectl apply -f deployment.yaml

echo ""
echo "Deployment complete! Checking pod status..."
kubectl get pods -l app=pod-monitor

echo ""
echo "To view logs: kubectl logs -l app=pod-monitor -f"
