# etcd-awx-sync

Tools for synchronizing host information from etcd service discovery to AWX (Ansible Tower) inventory. Includes both a CLI tool and an AI-powered Slack bot.

## Components

| Component | Description |
|-----------|-------------|
| `etcd_to_awx.py` | CLI tool for direct sync operations |
| `slack-mcp-agent` | AI-powered Slack bot with natural language support |

## Current Statistics

| Metric | Count |
|--------|-------|
| Total Hosts | ~2,900+ |
| Domains/Customers | 142 |
| Discovered Roles | 56 |

Top roles: `mphpp` (915), `os` (335), `mim` (286), `www` (241), `mphhos` (140), `ts` (127)

---

# Part 1: CLI Tool (etcd_to_awx.py)

## Features

- **Dynamic Discovery**: Automatically discovers all domains and roles from etcd
- **Smart Prompt Mode**: Create inventories using natural language
- **Flexible Filtering**: Filter by domain, role, or both
- **Auto Host Groups**: Creates groups based on customer, role, location, cluster

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials

# Smart prompt mode
python3 etcd_to_awx.py --smart

# Direct prompt
python3 etcd_to_awx.py --prompt "mphpp for pubwxp"

# CLI flags
python3 etcd_to_awx.py --role mphpp --domain pubwxp
```

## CLI Usage

```bash
# Full sync (all hosts)
python3 etcd_to_awx.py --full

# List available domains/roles
python3 etcd_to_awx.py --list-domains
python3 etcd_to_awx.py --list-roles

# Filter by domain and/or role
python3 etcd_to_awx.py --domain pubwxp
python3 etcd_to_awx.py --role mphpp
python3 etcd_to_awx.py --role mphpp --domain pubwxp
```

---

# Part 2: Slack Bot (slack-mcp-agent)

An AI-powered Slack bot that handles infrastructure operations through natural language.

## Architecture

```
┌─────────────────┐
│   Slack User    │
│  "how many      │
│ mphpp in bnxp?" │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Slack API     │────►│  Slack Agent    │
│  (Socket Mode)  │     │   (agent.py)    │
└─────────────────┘     └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │   LLM    │ │   MCP    │ │  Health  │
              │  Client  │ │ Registry │ │  Server  │
              └────┬─────┘ └────┬─────┘ └──────────┘
                   │            │
                   ▼            ▼
              Parse Intent  Route to MCP
                   │            │
                   └─────┬──────┘
                         ▼
              ┌─────────────────────┐
              │   etcd-awx-sync     │
              │        MCP          │
              └──────────┬──────────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
         ┌─────────┐          ┌─────────┐
         │  etcd   │          │   AWX   │
         └─────────┘          └─────────┘
```

## Slack Commands

### Query Commands (no confirmation needed)
```
/agent list domains              # Show all domains with host counts
/agent list roles                # Show all roles with host counts
/agent list roles in bnxp        # Show roles in specific domain
/agent status                    # Show overall statistics
/agent how many mphpp does bnxp have    # Count specific role in domain
/agent how many hosts does lolxp have   # Count total hosts in domain
/agent how many domains have ngx        # Count domains with specific role
```

### Action Commands (requires confirmation)
```
/agent sync all inventory        # Full sync from etcd to AWX
/agent create mphpp for pubwxp   # Create filtered inventory
```

## Deployment

### Build and Push Docker Image

```bash
# Login to GAR
gcloud auth configure-docker us-east1-docker.pkg.dev

# Build for amd64 and push
docker buildx build --platform linux/amd64 \
  -t us-east1-docker.pkg.dev/unity-vivox-docker-registry/mcp-vivox-ops/slack-mcp-agent:latest \
  --push .
```

### Deploy to Kubernetes

```bash
# Create image pull secret
kubectl create secret docker-registry gcr-secret \
  --docker-server=us-east1-docker.pkg.dev \
  --docker-username=_json_key \
  --docker-password="$(cat /path/to/gcr-key.json)" \
  --docker-email=your-email@example.com

# Create Slack secrets
kubectl create secret generic slack-mcp-agent-secrets \
  --from-literal=SLACK_BOT_TOKEN=xoxb-your-token \
  --from-literal=SLACK_APP_TOKEN=xapp-your-token \
  --from-literal=SLACK_SIGNING_SECRET=your-secret

# Deploy
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml

# Restart after code changes
kubectl rollout restart deployment slack-mcp-agent
```

---

# Configuration

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ETCD_SERVER` | etcd server hostname | localhost |
| `ETCD_PORT` | etcd server port | 2379 |
| `ETCD_PREFIX` | etcd key prefix | /discovery/ |
| `AWX_SERVER` | AWX server hostname | localhost |
| `AWX_CLIENT_ID` | AWX OAuth client ID | - |
| `AWX_CLIENT_SECRET` | AWX OAuth client secret | - |
| `AWX_USERNAME` | AWX username | - |
| `AWX_PASSWORD` | AWX password | - |
| `SLACK_BOT_TOKEN` | Slack bot token (xoxb-...) | Required for bot |
| `SLACK_APP_TOKEN` | Slack app token (xapp-...) | Required for bot |
| `LLM_PROVIDER` | LLM provider (unity, anthropic, mock) | mock |

## Hostname Parsing

Hostnames are parsed using the pattern: `<role>-<domain>-<numbers>-<index>.vivox.com`

Examples:
- `mphhos-aptus2-010103-1.vivox.com` → role=mphhos, domain=aptus2
- `mim-bnxp-010101-2.vivox.com` → role=mim, domain=bnxp
- `ngx-dcuxp-010103-1.vivox.com` → role=ngx, domain=dcuxp

---

# Project Structure

```
etcd-awx-sync/
├── etcd_to_awx.py           # CLI sync tool
├── src/
│   ├── agent.py             # Slack bot agent
│   ├── llm_client.py        # LLM integration
│   └── mcps/
│       ├── base.py          # Base MCP class
│       ├── registry.py      # MCP registry
│       └── etcd_awx/
│           └── mcp.py       # etcd-awx MCP
├── k8s/
│   ├── deployment.yaml      # K8s deployment
│   ├── configmap.yaml       # Configuration
│   └── deploy.sh            # Deploy script
├── playbooks/
│   └── sync_inventory.yml   # AWX playbook
├── docs/
│   └── AWX_SETUP.md         # AWX setup guide
├── main.py                  # Bot entry point
├── Dockerfile
└── requirements.txt
```

---

# Troubleshooting

### ImagePullBackOff (K8s)
- Check gcr-secret exists: `kubectl get secret gcr-secret`
- Verify service account has `roles/storage.objectViewer`
- Ensure image is built for linux/amd64

### 0 domains/hosts returned
- Check etcd connectivity
- Verify ETCD_SERVER and ETCD_PORT
- Check ETCD_PREFIX matches your structure

### OAuth Token Error (AWX)
- Ensure AWX OAuth app uses "Resource Owner Password-Based" grant type
- Verify all 4 credentials are provided

---

## License

MIT
