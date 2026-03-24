# vops-bot

Slack-based infrastructure operations bot for managing services, running Ansible playbooks, scaling OpenStack resources, and monitoring Kubernetes pods. Built as a set of independently deployable microservices that the main bot auto-discovers at startup.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Slack (Socket Mode)               │
└──────────────────────┬──────────────────────────────┘
                       │
              ┌────────▼────────┐
              │  slack-mcp-agent │  ← Main bot, routes commands
              │    (port 8080)   │
              └──┬─────┬─────┬──┘
                 │     │     │
        ┌────────▼┐ ┌──▼───┐ ┌▼──────────┐
        │ service- │ │ pod- │ │    tf-     │
        │ manager  │ │monit.│ │  manager   │
        │ (8081)   │ │(8082)│ │  (8083)    │
        └──┬───┬───┘ └──┬───┘ └──┬────┬───┘
           │   │        │        │    │
        ┌──▼┐ ┌▼──┐  ┌──▼──┐ ┌──▼┐ ┌─▼──┐
        │SSH│ │AWX│  │K8s  │ │TFC│ │Git │
        │   │ │API│  │ API │ │API│ │Hub │
        └───┘ └───┘  └─────┘ └───┘ └────┘
              etcd ◄──── host discovery
```

The system follows the **MCP pattern**. Each microservice exposes a `/info` endpoint describing its available actions and parameters. The main bot discovers these at startup via environment variables (`REMOTE_MCP_SERVICE_MANAGER=http://service-manager:8081`, etc.), calls `/info` on each, and builds a routing table. When a Slack command arrives, the bot routes it to the correct service over HTTP. Services can be added, removed, or restarted independently without affecting each other.

## Services

### slack-mcp-agent (Main Bot)

The central orchestrator. Connects to Slack via Socket Mode, receives slash commands (`/svc`, `/awx`, `/tf`, `/pods`), parses user intent, routes requests to the appropriate microservice, and posts results back to Slack. Also handles interactive button callbacks (pod alert actions, Terraform confirm/cancel).

- **Port:** 8080
- **Key files:** `main.py`, `src/agent.py`, `src/llm_client.py`, `src/request_queue.py`
- **Image:** `us-east1-docker.pkg.dev/unity-vivox-docker-registry/mcp-vivox-ops/slack-mcp-agent:latest`

### service-manager

Manages services across hosts via SSH. Queries **etcd** to discover hosts by role and domain, opens concurrent SSH connections, and runs `systemctl` commands in parallel. For Azure hosts (IP range `10.253.x.x`), routes commands through the **AWX ad-hoc API** instead of direct SSH due to network constraints.

- **Port:** 8081
- **Key files:** `services/service-manager/src/mcp.py`, `src/ssh_client.py`, `src/awx_client.py`
- **Host discovery:** etcd at `10.0.25.44:2379` — no hardcoded host lists; hosts are available immediately when added to etcd
- **SSH auth:** `root` + password for regular hosts, `vivoxops` + password + sudo for Azure hosts

**Supported actions:** `check-service`, `restart-service`, `start-service`, `stop-service`, `service-logs`, `get-version`, `list-service-roles`

**Supported roles:** mim, mphpp, mphhos, ts, www, www5, ngx, ngxint, redis, mongodb, tps, harjo, hamim, haweb, srouter, sdecoder, scapture, ser, sconductor, mimmem, provnstatdb5

### pod-monitor

Monitors Kubernetes pod health and provides both active queries and passive alerting.

- **Port:** 8082
- **Key files:** `services/pod-monitor/src/mcp.py`, `src/k8s_client.py`, `src/alerter.py`
- **RBAC:** Uses a `pod-monitor` ServiceAccount with read-only ClusterRole for pods, logs, events, and metrics

**Active actions:** `list-pods`, `pod-details`, `pod-logs`, `unhealthy-pods`, `namespace-summary`

**Passive alerting:** A background task checks pod health every 2 minutes. Detected conditions include CrashLoopBackOff, OOMKilled, high restarts (only if the last restart was within 30 minutes), stuck Pending (>5 min), ImagePullBackOff, and not-ready (>5 min, using the `Ready` condition's `lastTransitionTime`).

Alerts use the **Slack Web API** with Block Kit messages. The first alert for an issue posts a message with interactive buttons (Resolve, Pause 1d, Pause 1w). Subsequent alerts for the same issue are posted as **threaded replies** under the original message. When a pod self-heals, the alerter posts "Auto-resolved" in the thread. Button clicks are handled by `slack-mcp-agent`, which calls pod-monitor's HTTP endpoints (`/alert/resolve`, `/alert/pause`) to update alert state.

### tf-manager

Manages Terraform-driven OpenStack scaling. Handles the full lifecycle: git pull, `.tf` file modification, commit and push, Terraform Cloud plan polling (including Sentinel policies), user confirmation via Slack buttons, TFC apply, and post-apply verification by SSHing through a jump host to query OpenStack for actual server IPs.

- **Port:** 8083
- **Key files:** `services/tf-manager/src/mcp.py`, `src/tfc_client.py`, `src/tf_parser.py`, `src/git_client.py`, `src/openstack_client.py`

**Supported actions:** `add-servers`, `remove-servers`, `confirm-apply`, `cancel-run`, `show-domain`, `list-domains`

## Commands

### `/svc` — Service Management

```
/svc check mim in lionamxp        # Check service status across all matching hosts
/svc check mim in lionamxp host 5 # Check a specific host only
/svc restart mim on pubwxp        # Restart service (with confirmation)
/svc start mim on pubwxp          # Start service
/svc stop mim on pubwxp           # Stop service
/svc logs mim in lionamxp host 3  # Get logs from a specific host
/svc version mim in pubwxp        # Get software version from etcd
/svc list                         # List all supported roles
```

Discovers hosts from etcd, opens concurrent SSH connections, runs the command on all matching hosts in parallel, and returns aggregated results.

**Restart confirmation:** All restarts require a Yes/Cancel confirmation showing the host count. Critical roles (`mim`, `mphpp`, `mphhos`) get an additional second step asking whether to post a notification to `#vivox-ops-notification` before restarting. Only the user who initiated the restart can click the buttons.

### `/awx` — Playbook Execution

```
/awx run syslog-troubleshooter on mim-lionamxp   # Run playbook on inventory
/awx run check-service globally                   # Run against all ~4500 hosts
/awx list playbooks                               # List available playbooks
/awx set repo vivox-ops-ansible                   # Switch GitHub repo preset
/awx queue status                                 # Check job queue
/awx job status 285                               # Check specific job
```

Supports two GitHub repo presets (internal GHE and public GitHub) with auto-switching — if a playbook isn't found in the active repo, it checks the other one automatically. Azure hosts are auto-detected by IP range and SSH credentials are injected at job launch. Global mode runs against the central inventory with progress updates every 30 seconds and a 60-minute timeout. Jobs are queued with deduplication.

### `/tf` — Terraform Scaling

```
/tf add 2 mphpp to aptus2         # Scale up (shows plan, waits for confirm)
/tf remove 1 mphpp from aptus2    # Scale down
/tf confirm <run_id>              # Apply a pending plan
/tf cancel <run_id>               # Cancel a pending plan
/tf show aptus2                   # Show current resource counts
```

Modifies `.tf` files in the `vivox-ops-openstack` repo, pushes to main, polls Terraform Cloud for plan results, and waits for explicit user confirmation before applying.

### `/pods` — Kubernetes Monitoring

```
/pods                             # List all pods
/pods unhealthy                   # Show only failing pods
/pods details <pod-name>          # Detailed info (fuzzy match supported)
/pods logs <pod-name>             # Recent container logs
/pods logs <pod-name> 50          # Last 50 lines
/pods summary                     # Namespace overview with resource totals
```

## Tech Stack

| Layer | Technology |
|-------|------------|
| Bot framework | Slack Bolt (Socket Mode) |
| Language | Python 3.11, asyncio |
| Container | Docker, Google Artifact Registry |
| Orchestration | Kubernetes |
| Host discovery | etcd |
| Configuration mgmt | AWX (Ansible Tower) |
| Infrastructure as Code | Terraform Cloud |
| Cloud | OpenStack (via TFC), Azure (via AWX) |
| Inter-service | aiohttp (REST) |
| SSH | asyncssh (direct), AWX ad-hoc API (Azure) |

## Setup

```bash
git clone https://github.cds.internal.unity3d.com/anubhav-arya/vops-bot.git
cd vops-bot
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your credentials (etcd, AWX, Slack tokens, SSH passwords)

python3 main.py
```

### Kubernetes Deployment

Each service has its own `k8s/` directory with `configmap.yaml`, `deployment.yaml`, and `deploy.sh`:

```bash
# Deploy main bot
k8s/deploy.sh

# Deploy a microservice
services/service-manager/k8s/deploy.sh
services/pod-monitor/k8s/deploy.sh
services/tf-manager/k8s/deploy.sh
```

### Build & Push

All images target `linux/amd64` for the K8s cluster:

```bash
# Main bot
docker buildx build --platform linux/amd64 \
  -t us-east1-docker.pkg.dev/unity-vivox-docker-registry/mcp-vivox-ops/slack-mcp-agent:latest --push .

# Microservices
docker buildx build --platform linux/amd64 \
  -t us-east1-docker.pkg.dev/unity-vivox-docker-registry/mcp-vivox-ops/service-manager:latest --push services/service-manager

docker buildx build --platform linux/amd64 \
  -t us-east1-docker.pkg.dev/unity-vivox-docker-registry/mcp-vivox-ops/pod-monitor:latest --push services/pod-monitor

docker buildx build --platform linux/amd64 \
  -t us-east1-docker.pkg.dev/unity-vivox-docker-registry/mcp-vivox-ops/tf-manager:latest --push services/tf-manager
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — Component details, data flow, adding new MCPs
- [AWX Setup](docs/AWX_SETUP.md) — AWX project and job template configuration
- [Slack Setup](docs/SLACK_SETUP.md) — Slack app creation and token configuration
- [Playbook Standards](docs/PLAYBOOK_STANDARDS.md) — Ansible playbook conventions

## License

MIT
