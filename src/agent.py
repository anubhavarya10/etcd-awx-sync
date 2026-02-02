"""Main Slack Agent that orchestrates MCPs using LLM for intent parsing."""

import os
import re
import asyncio
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from .llm_client import BaseLLMClient, create_llm_client, IntentResult
from .mcps import MCPRegistry, MCPResult, MCPResultStatus, get_registry

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for the Slack Agent."""
    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: Optional[str] = None
    default_channel_id: Optional[str] = None
    llm_provider: str = "unity"
    llm_model: Optional[str] = None
    log_level: str = "INFO"


class SlackMCPAgent:
    """
    AI-powered Slack agent that handles user requests through MCPs.

    The agent:
    1. Receives messages/commands from Slack
    2. Uses LLM to parse user intent
    3. Routes to appropriate MCP
    4. Handles confirmations and responses
    """

    def __init__(
        self,
        config: AgentConfig,
        llm_client: Optional[BaseLLMClient] = None,
        registry: Optional[MCPRegistry] = None,
    ):
        self.config = config
        self.registry = registry or get_registry()
        self.llm_client = llm_client or create_llm_client(
            provider=config.llm_provider,
        )

        # Initialize Slack app
        self.app = AsyncApp(
            token=config.slack_bot_token,
            signing_secret=config.slack_signing_secret,
        )

        # Register event handlers
        self._setup_handlers()

        logger.info("SlackMCPAgent initialized")

    def _setup_handlers(self):
        """Set up Slack event handlers."""

        @self.app.command("/agent")
        async def handle_agent_command(ack, command, client, respond):
            """Handle /agent slash command."""
            await ack()
            text = command.get("text", "")
            channel_id = command.get("channel_id")
            user_id = command.get("user_id")
            logger.info(f"Received /agent command: '{text}' from user {user_id}")

            # Create a public responder that posts to channel
            async def public_respond(**kwargs):
                kwargs["channel"] = channel_id
                if "text" not in kwargs:
                    kwargs["text"] = "Response"
                await client.chat_postMessage(**kwargs)

            try:
                await self._process_message(
                    text=text,
                    user_id=user_id,
                    channel_id=channel_id,
                    client=client,
                    respond=public_respond,
                )
            except Exception as e:
                logger.exception(f"Error handling /agent command: {e}")
                await client.chat_postMessage(channel=channel_id, text=f"Error: {str(e)}")

        @self.app.command("/inventory")
        async def handle_inventory_command(ack, command, client, respond):
            """Handle /inventory slash command (backward compatible)."""
            await ack()
            text = command.get("text", "")
            channel_id = command.get("channel_id")
            user_id = command.get("user_id")

            # Create a public responder that posts to channel
            async def public_respond(**kwargs):
                kwargs["channel"] = channel_id
                if "text" not in kwargs:
                    kwargs["text"] = "Response"
                await client.chat_postMessage(**kwargs)

            await self._process_message(
                text=f"inventory {text}" if text else "inventory help",
                user_id=user_id,
                channel_id=channel_id,
                client=client,
                respond=public_respond,
            )

        @self.app.event("app_mention")
        async def handle_mention(event, say, client):
            """Handle @mentions of the bot."""
            text = event.get("text", "")
            # Remove the bot mention from the text
            text = " ".join(
                word for word in text.split()
                if not word.startswith("<@")
            )

            await self._process_message(
                text=text,
                user_id=event.get("user"),
                channel_id=event.get("channel"),
                client=client,
                respond=say,
            )

        @self.app.event("message")
        async def handle_dm(event, say, client):
            """Handle direct messages to the bot."""
            # Only respond to DMs
            if event.get("channel_type") == "im":
                # Ignore bot's own messages
                if event.get("bot_id"):
                    return

                await self._process_message(
                    text=event.get("text", ""),
                    user_id=event.get("user"),
                    channel_id=event.get("channel"),
                    client=client,
                    respond=say,
                )

        @self.app.action(re.compile(r"confirm_.*"))
        async def handle_confirm(ack, action, body, client, respond):
            """Handle confirmation button clicks."""
            await ack()
            action_id = action.get("value")
            user_id = body.get("user", {}).get("id")

            result = await self.registry.handle_confirmation(
                action_id=action_id,
                confirmed=True,
                user_id=user_id,
            )

            await self._send_result(result, respond, client, body.get("channel", {}).get("id"))

        @self.app.action(re.compile(r"cancel_.*"))
        async def handle_cancel(ack, action, body, client, respond):
            """Handle cancel button clicks."""
            await ack()
            action_id = action.get("value")
            user_id = body.get("user", {}).get("id")

            result = await self.registry.handle_confirmation(
                action_id=action_id,
                confirmed=False,
                user_id=user_id,
            )

            await respond(text="Action cancelled.")

        # Catch-all for events we don't need to handle (suppress warnings)
        @self.app.event("channel_archive")
        @self.app.event("channel_unarchive")
        @self.app.event("channel_created")
        @self.app.event("channel_deleted")
        @self.app.event("channel_rename")
        @self.app.event("member_joined_channel")
        @self.app.event("member_left_channel")
        @self.app.event("user_change")
        @self.app.event("team_join")
        async def handle_ignored_events(event, logger):
            """Silently ignore these events."""
            pass

    async def _process_message(
        self,
        text: str,
        user_id: str,
        channel_id: str,
        client: AsyncWebClient,
        respond,
    ):
        """Process an incoming message using LLM for intent parsing."""
        if not text.strip():
            await respond(text=self._get_help_message())
            return

        text_lower = text.lower().strip()

        # Handle simple help command without LLM
        if text_lower in ["help", "?", "hi", "hello"]:
            await respond(text=self._get_help_message())
            return

        # Handle list MCPs command
        if text_lower in ["list mcps", "mcps", "list"]:
            await respond(text=self.registry.get_mcp_list_for_llm())
            return

        # Use LLM to parse intent
        try:
            await respond(text=f"*Query:* `{text}`\n_Processing..._")

            mcp_context = self.registry.get_llm_context()
            intent = await self.llm_client.parse_intent(
                user_message=text,
                mcp_context=mcp_context,
            )

            logger.info(f"Parsed intent: mcp={intent.mcp_name}, action={intent.action}, "
                       f"params={intent.parameters}, confidence={intent.confidence}")

            # Handle unknown intent
            if intent.mcp_name == "unknown" or intent.confidence < 0.5:
                await respond(
                    text=f"*Query:* `{text}`\n\n"
                         f"I'm not sure what you want to do. {intent.explanation}\n\n"
                         f"Try asking for `help` or be more specific."
                )
                return

            # Route to MCP
            result = await self.registry.route_action(
                mcp_name=intent.mcp_name,
                action=intent.action,
                parameters=intent.parameters,
                user_id=user_id,
                channel_id=channel_id,
            )

            await self._send_result(result, respond, client, channel_id)

        except Exception as e:
            logger.exception("Error processing message")
            await respond(text=f"Error: {str(e)}")

    async def _send_result(
        self,
        result: MCPResult,
        respond,
        client: AsyncWebClient,
        channel_id: str,
    ):
        """Send an MCP result back to Slack."""
        if result.status == MCPResultStatus.NEEDS_CONFIRMATION:
            # Send confirmation prompt with buttons
            if result.blocks:
                await respond(text=result.message, blocks=result.blocks)
            else:
                await respond(text=result.confirmation_prompt or result.message)
        else:
            # Send regular message
            msg = result.to_slack_message()
            await respond(**msg)

    def _get_help_message(self) -> str:
        """Generate help message."""
        mcp_list = self.registry.get_mcp_list_for_llm()

        return f"""*Slack MCP Agent*

I'm an AI-powered assistant that can help you manage infrastructure operations.

*How to use:*
- Just describe what you want to do in natural language
- I'll figure out which tool to use and confirm before taking action

*Example commands:*
- `sync all inventory from etcd to awx`
- `create inventory for mphpp servers in pubwxp`
- `list available domains`
- `list roles`

{mcp_list}

*Commands:*
- `/agent <request>` - Send a request
- `/inventory <request>` - Inventory-specific requests
- `@agent <request>` - Mention me in a channel
- DM me directly

Need more help? Just ask!
"""

    async def start(self):
        """Start the Slack agent."""
        handler = AsyncSocketModeHandler(self.app, self.config.slack_app_token)
        logger.info("Starting Slack MCP Agent in Socket Mode...")
        await handler.start_async()

    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check for K8s probes.

        Returns:
            Dict with health status of all components
        """
        mcp_health = await self.registry.health_check()

        return {
            "status": "healthy" if all(mcp_health.values()) else "degraded",
            "mcps": mcp_health,
        }


def create_agent_from_env() -> SlackMCPAgent:
    """Create an agent instance from environment variables."""
    config = AgentConfig(
        slack_bot_token=os.environ["SLACK_BOT_TOKEN"],
        slack_app_token=os.environ["SLACK_APP_TOKEN"],
        slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
        default_channel_id=os.environ.get("SLACK_CHANNEL_ID"),
        llm_provider=os.environ.get("LLM_PROVIDER", "unity"),
        llm_model=os.environ.get("LLM_MODEL"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )

    return SlackMCPAgent(config)
