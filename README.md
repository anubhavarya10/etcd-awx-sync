# Slack MCP Agent

An AI-powered Slack bot that handles infrastructure operations through modular MCP (Model Context Protocol) handlers.

## Features

- **AI-Powered Intent Parsing**: Uses LLM (Unity AI / Claude) to understand natural language requests
- **Modular MCP Architecture**: Easily add new capabilities through MCP plugins
- **Confirmation Workflow**: Confirms destructive actions before execution
- **Kubernetes Native**: Designed to run in K8s with health checks and proper lifecycle management
- **Extensible**: Add new MCPs for Ansible, AWS, GCP, and more

## Architecture

```
┌─────────────────┐
│   Slack User    │
│  "sync mphpp    │
│   for pubwxp"   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Slack API     │────►│  Slack Agent    │
│  (Socket Mode)  │     │   (main.py)     │
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

## Quick Start

### Local Development

```bash
# Clone the repository
git clone https://github.com/your-org/slack-mcp-agent.git
cd slack-mcp-agent

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your credentials

# Clone etcd-awx-sync (or set ETCD_AWX_SYNC_PATH)
git clone https://github.com/your-org/etcd-awx-sync.git /path/to/etcd-awx-sync
export ETCD_AWX_SYNC_PATH=/path/to/etcd-awx-sync

# Run the agent
python main.py
```

### Kubernetes Deployment

```bash
# Configure secrets (edit k8s/secret.yaml with your values)
# WARNING: Never commit actual secrets to git!

# Deploy
kubectl apply -k k8s/

# Check status
kubectl get pods -l app=slack-mcp-agent
kubectl logs -l app=slack-mcp-agent -f
```

## Usage

### Natural Language Commands

Just describe what you want to do:

```
@agent sync all inventory from etcd to awx
@agent create inventory for mphpp servers in pubwxp
@agent list available domains
@agent what roles are available?
@agent status
```

### Slash Commands

```
/agent sync all hosts
/agent create mphpp for pubwxp
/agent list domains
/inventory create mphpp-pubwxp  # backward compatible
```

### Direct Messages

DM the bot with your request - no @ mention needed.

## MCPs (Model Context Protocols)

MCPs are modular handlers that provide specific capabilities.

### Available MCPs

| MCP | Description | Actions |
|-----|-------------|---------|
| `etcd-awx-sync` | Sync hosts from etcd to AWX | sync, create, list-domains, list-roles, status |

### Adding New MCPs

1. Create a new directory under `src/mcps/`
2. Implement `BaseMCP` class
3. Register in `main.py`

```python
# src/mcps/my_mcp/mcp.py
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
            examples=["do the thing", "run my action"],
        ))

    async def execute(self, action, parameters, user_id, channel_id):
        # Implementation
        pass
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SLACK_BOT_TOKEN` | Slack bot token (xoxb-...) | Required |
| `SLACK_APP_TOKEN` | Slack app token for Socket Mode (xapp-...) | Required |
| `SLACK_SIGNING_SECRET` | Slack signing secret | Optional |
| `SLACK_CHANNEL_ID` | Default channel for alerts | Optional |
| `LLM_PROVIDER` | LLM provider (unity, anthropic, mock) | unity |
| `UNITY_AI_API_KEY` | Unity AI API key | Required if provider=unity |
| `UNITY_AI_API_BASE` | Unity AI API base URL | https://api.unity.ai/v1 |
| `ETCD_SERVER` | etcd server hostname | localhost |
| `ETCD_PORT` | etcd server port | 2379 |
| `AWX_SERVER` | AWX server hostname | localhost |
| `LOG_LEVEL` | Logging level | INFO |

## Development

### Project Structure

```
slack-mcp-agent/
├── src/
│   ├── __init__.py
│   ├── agent.py          # Main Slack agent
│   ├── llm_client.py     # LLM integration
│   └── mcps/
│       ├── __init__.py
│       ├── base.py       # Base MCP class
│       ├── registry.py   # MCP registry
│       └── etcd_awx/     # etcd-awx-sync MCP
│           ├── __init__.py
│           └── mcp.py
├── k8s/
│   ├── deployment.yaml
│   ├── configmap.yaml
│   ├── secret.yaml
│   └── kustomization.yaml
├── main.py               # Entry point
├── Dockerfile
├── requirements.txt
└── README.md
```

### Testing

```bash
# Run with mock LLM (no API key needed)
export LLM_PROVIDER=mock
python main.py
```

## Roadmap

- [ ] Ansible playbook execution MCP
- [ ] AWS operations MCP
- [ ] Terraform MCP
- [ ] Vault secrets MCP
- [ ] Conversation context/memory
- [ ] Rate limiting
- [ ] Audit logging

## License

MIT
