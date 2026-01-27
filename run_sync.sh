#!/bin/bash
# Wrapper script for cron job execution
# Edit the path below to match your installation

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Run the sync
echo "========================================"
echo "Starting etcd to AWX sync at $(date)"
echo "========================================"

python3 etcd_to_awx.py

echo "========================================"
echo "Sync completed at $(date)"
echo "========================================"
