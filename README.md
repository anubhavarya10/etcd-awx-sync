# Slack MCP Agent

An AI-powered Slack bot that handles infrastructure operations through modular MCP (Model Context Protocol) handlers. Currently integrates with etcd service discovery and AWX for inventory management.

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

## Features

- **Natural Language Understanding**: Uses LLM to parse user requests (Unity AI, Anthropic, or Mock for testing)
- **Modular MCP Architecture**: Pluggable handlers for different operations
- **Confirmation Workflow**: Confirms destructive actions before execution
- **Kubernetes Native**: Health checks, proper lifecycle management
- **etcd Integration**: Reads host data from etcd service discovery
- **Hostname Parsing**: Parses `<role>-<domain>-<numbers>-<index>.vivox.com` pattern

## Available Commands

### Query Commands (no confirmation needed)
```
/agent list domains              # Show all domains with host counts
/agent list roles                # Show all roles with host counts
/agent list roles in bnxp        # Show roles in specific domain
/agent list domains for mphpp    # Show domains with specific role
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

### Prerequisites

- Docker with buildx (for multi-platform builds)
- Access to Google Artifact Registry (GAR)
- Kubernetes cluster (on-prem or cloud)
- etcd server with service discovery data

### Build and Push Docker Image

```bash
# Login to GAR (one-time setup)
gcloud auth configure-docker us-east1-docker.pkg.dev

# Build for amd64 (required for most K8s clusters) and push
docker buildx build --platform linux/amd64 \
  -t us-east1-docker.pkg.dev/unity-vivox-docker-registry/mcp-vivox-ops/slack-mcp-agent:latest \
  --push .
```

### Deploy to Kubernetes

1. **Create image pull secret** (for pulling from GAR):
```bash
# Create service account key in GCP Console, then:
kubectl create secret docker-registry gcr-secret \
  --docker-server=us-east1-docker.pkg.dev \
  --docker-username=_json_key \
  --docker-password="$(cat /path/to/gcr-key.json)" \
  --docker-email=your-email@example.com
```

2. **Create Slack secrets**:
```bash
kubectl create secret generic slack-mcp-agent-secrets \
  --from-literal=SLACK_BOT_TOKEN=xoxb-your-token \
  --from-literal=SLACK_APP_TOKEN=xapp-your-token \
  --from-literal=SLACK_SIGNING_SECRET=your-secret
```

3. **Deploy**:
```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml

# Or use the deploy script
cd k8s && ./deploy.sh
```

4. **Verify**:
```bash
kubectl get pods -l app=slack-mcp-agent
kubectl logs -l app=slack-mcp-agent -f
```

### Restart after code changes

```bash
# Rebuild and push image
docker buildx build --platform linux/amd64 \
  -t us-east1-docker.pkg.dev/unity-vivox-docker-registry/mcp-vivox-ops/slack-mcp-agent:latest \
  --push .

# Restart pod to pull new image
kubectl rollout restart deployment slack-mcp-agent
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SLACK_BOT_TOKEN` | Slack bot token (xoxb-...) | Required |
| `SLACK_APP_TOKEN` | Slack app token for Socket Mode (xapp-...) | Required |
| `SLACK_SIGNING_SECRET` | Slack signing secret | Optional |
| `LLM_PROVIDER` | LLM provider (unity, anthropic, mock) | mock |
| `UNITY_AI_API_KEY` | Unity AI API key | Required if provider=unity |
| `ETCD_SERVER` | etcd server hostname | localhost |
| `ETCD_PORT` | etcd server port | 2379 |
| `ETCD_PREFIX` | etcd key prefix | /discovery/ |
| `AWX_SERVER` | AWX server hostname | localhost |
| `LOG_LEVEL` | Logging level | INFO |

## Project Structure

```
slack-mcp-agent/
├── src/
│   ├── agent.py           # Main Slack agent with event handlers
│   ├── llm_client.py      # LLM integration (Unity AI, Anthropic, Mock)
│   └── mcps/
│       ├── base.py        # Base MCP class and types
│       ├── registry.py    # MCP registry for routing
│       └── etcd_awx/
│           └── mcp.py     # etcd-awx-sync MCP implementation
├── k8s/
│   ├── deployment.yaml    # K8s deployment with health checks
│   ├── configmap.yaml     # Non-sensitive configuration
│   ├── secret.yaml.template  # Template for secrets
│   └── deploy.sh          # Deployment script
├── main.py                # Entry point
├── Dockerfile
└── requirements.txt
```

## Hostname Parsing

The etcd-awx MCP parses hostnames from etcd keys using this pattern:
```
<role>-<domain>-<numbers>-<index>.vivox.com
```

Examples:
- `mphhos-aptus2-010103-1.vivox.com` → role=mphhos, domain=aptus2
- `mim-bnxp-010101-2.vivox.com` → role=mim, domain=bnxp
- `ngx-dcuxp-010103-1.vivox.com` → role=ngx, domain=dcuxp

## Adding New MCPs

1. Create directory under `src/mcps/`
2. Implement `BaseMCP` class
3. Register in `src/mcps/__init__.py`

```python
from ..base import BaseMCP, MCPAction, MCPResult

class MyMCP(BaseMCP):
    @property
    def name(self) -> str:
        return "my-mcp"

    @property
    def description(self) -> str:
        return "My custom MCP"

    def _setup_actions(self):
        self.register_action(MCPAction(
            name="my-action",
            description="Does something useful",
            examples=["do the thing"],
        ))

    async def execute(self, action, parameters, user_id, channel_id):
        return MCPResult(status=MCPResultStatus.SUCCESS, message="Done!")
```

## Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run with mock LLM (no API key needed)
export LLM_PROVIDER=mock
export SLACK_BOT_TOKEN=xoxb-your-token
export SLACK_APP_TOKEN=xapp-your-token
python main.py
```

## Troubleshooting

### ImagePullBackOff
- Check gcr-secret exists: `kubectl get secret gcr-secret`
- Verify service account has `roles/storage.objectViewer` permission
- Ensure image is built for correct architecture (linux/amd64)

### 0 domains/hosts returned
- Check etcd connectivity from pod
- Verify ETCD_SERVER and ETCD_PORT in configmap
- Check ETCD_PREFIX matches your etcd structure

### Unhandled Slack events
- These warnings are normal - the bot ignores channel_archive, member_joined, etc.

## License

MIT
