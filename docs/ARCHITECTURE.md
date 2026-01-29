# Architecture Documentation

## Overview

The Slack MCP Agent is an AI-powered Slack bot that uses Large Language Models (LLMs) to understand natural language requests and routes them to appropriate MCP (Model Context Protocol) handlers.

## Core Concepts

### MCP (Model Context Protocol)

MCPs are modular handlers that encapsulate specific functionality. Each MCP:

- Defines available actions and their parameters
- Provides context for LLM understanding
- Handles execution and confirmation workflows
- Reports results back to users

### LLM Integration

The agent uses an LLM (Unity AI / Claude) to:

1. Parse natural language into structured intents
2. Extract relevant parameters
3. Determine confidence levels
4. Generate helpful responses

### Confirmation Workflow

For destructive or long-running operations:

1. User makes request
2. LLM parses intent
3. MCP creates confirmation prompt with Slack buttons
4. User confirms or cancels
5. MCP executes if confirmed

## Component Details

### Agent (`src/agent.py`)

The main orchestrator that:

- Initializes Slack app with event handlers
- Routes messages through LLM for intent parsing
- Dispatches to appropriate MCP
- Handles confirmation callbacks
- Manages error responses

Key methods:
- `_process_message()` - Main message processing pipeline
- `_send_result()` - Format and send MCP results to Slack
- `health_check()` - K8s readiness probe

### LLM Client (`src/llm_client.py`)

Abstraction layer for LLM providers:

- `BaseLLMClient` - Abstract base class
- `UnityAIClient` - Unity AI (OpenAI-compatible) implementation
- `AnthropicClient` - Direct Anthropic API implementation
- `MockLLMClient` - Testing without API calls

Key methods:
- `parse_intent()` - Extract structured intent from natural language
- `generate_response()` - Generate conversational responses

### MCP Base (`src/mcps/base.py`)

Base class for all MCPs:

- `MCPAction` - Action definition with parameters and examples
- `MCPResult` - Operation result with status and Slack formatting
- `BaseMCP` - Abstract base class with confirmation workflow

Key methods:
- `_setup_actions()` - Register available actions (abstract)
- `execute()` - Execute an action (abstract)
- `create_confirmation()` - Create confirmation prompt with buttons
- `handle_confirmation()` - Process user confirmation response

### MCP Registry (`src/mcps/registry.py`)

Central registry for MCP management:

- Stores all registered MCPs
- Provides combined LLM context
- Routes actions to appropriate MCP
- Handles confirmation callbacks

Key methods:
- `register()` / `unregister()` - MCP lifecycle
- `get_llm_context()` - Combined context for LLM
- `route_action()` - Dispatch to correct MCP
- `health_check()` - Aggregate health status

## Data Flow

### Request Processing

```
1. Slack Event (message/command/mention)
        │
        ▼
2. Agent receives event
        │
        ▼
3. LLM parses intent
        │
        ├── Confidence < 0.5 → Return help message
        │
        ▼
4. Registry routes to MCP
        │
        ├── Unknown MCP → Return error
        │
        ▼
5. MCP.execute()
        │
        ├── Needs confirmation → Return confirmation prompt
        │
        ▼
6. Return result to Slack
```

### Confirmation Flow

```
1. MCP.execute() returns NEEDS_CONFIRMATION
        │
        ▼
2. Agent sends message with Confirm/Cancel buttons
        │
        ▼
3. User clicks button
        │
        ▼
4. Slack sends action callback
        │
        ▼
5. Agent calls registry.handle_confirmation()
        │
        ▼
6. Registry finds owning MCP
        │
        ▼
7. MCP._execute_confirmed() runs actual operation
        │
        ▼
8. Return final result to Slack
```

## LLM Prompt Engineering

### Intent Parsing Prompt

The system prompt for intent parsing includes:

1. Role definition (routing assistant)
2. Available MCPs and their actions
3. Parameter definitions
4. Example prompts for each action
5. Strict JSON output format

### Context Format

MCPs provide context in this format:

```
MCP: mcp-name
Description: What this MCP does

Available Actions:
- action1: Description
  - param1 (required): description
  - param2 (optional): description
  Examples: "example 1", "example 2"
```

## Kubernetes Integration

### Health Checks

- **Liveness** (`/health`): Always returns 200 if process is running
- **Readiness** (`/ready`): Checks all MCP health status

### Resource Management

- Memory: 256Mi request, 512Mi limit
- CPU: 100m request, 500m limit
- Single replica (Socket Mode doesn't support multiple)

### Secrets Management

Secrets are managed via K8s secrets, never committed to git.

## Adding New MCPs

### Step 1: Create MCP Class

```python
# src/mcps/my_mcp/mcp.py
from ..base import BaseMCP, MCPAction, MCPResult, MCPResultStatus

class MyMCP(BaseMCP):
    @property
    def name(self) -> str:
        return "my-mcp"

    @property
    def description(self) -> str:
        return "Description for LLM context"

    def _setup_actions(self):
        self.register_action(MCPAction(
            name="my-action",
            description="What this action does",
            parameters=[
                {"name": "param1", "type": "string", "required": True}
            ],
            requires_confirmation=True,
            examples=["natural language example 1", "example 2"],
        ))

    async def execute(self, action, parameters, user_id, channel_id):
        if action == "my-action":
            return self.create_confirmation(
                action=action,
                parameters=parameters,
                user_id=user_id,
                channel_id=channel_id,
                confirmation_message="Confirm this action?"
            )
        return MCPResult(status=MCPResultStatus.ERROR, message="Unknown action")

    async def _execute_confirmed(self, action, parameters, user_id, channel_id):
        # Do the actual work
        return MCPResult(status=MCPResultStatus.SUCCESS, message="Done!")

    async def health_check(self) -> bool:
        # Check dependencies
        return True
```

### Step 2: Register in main.py

```python
from src.mcps.my_mcp import MyMCP

# In main()
register_mcp(MyMCP())
```

## Security Considerations

1. **Token Security**: Never log or expose Slack/API tokens
2. **Confirmation Workflow**: All destructive actions require user confirmation
3. **Input Validation**: MCPs validate parameters before execution
4. **Audit Trail**: All actions are logged with user IDs
5. **Rate Limiting**: (Future) Implement per-user rate limiting

## Future Enhancements

1. **Conversation Memory**: Maintain context across messages
2. **Multi-step Workflows**: Chain actions together
3. **Scheduled Actions**: Cron-style scheduled operations
4. **Role-based Access**: Restrict actions by Slack user/group
5. **Metrics/Observability**: Prometheus metrics, distributed tracing
