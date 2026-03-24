"""Main Slack Agent that orchestrates MCPs using LLM for intent parsing."""

import os
import re
import asyncio
import logging
import time
import uuid
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from .llm_client import BaseLLMClient, create_llm_client, IntentResult
from .mcps import MCPRegistry, MCPResult, MCPResultStatus, get_registry

logger = logging.getLogger(__name__)

RESTART_CONFIRM_ROLES = {"mim", "mphpp", "mphhos"}
ROLE_DISPLAY_NAMES = {"mim": "mongooseim", "mphpp": "morpheus", "mphhos": "morpheus"}


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

        # Pending restart confirmations: restart_id -> state dict
        self._pending_restarts: Dict[str, Dict[str, Any]] = {}

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

        @self.app.command("/svc")
        async def handle_svc_command(ack, command, client, respond):
            """Handle /svc slash command for service management."""
            await ack()
            text = command.get("text", "").strip()
            channel_id = command.get("channel_id")
            user_id = command.get("user_id")
            logger.info(f"Received /svc command: '{text}' from user {user_id}")

            # Create a public responder that posts to channel
            async def public_respond(**kwargs):
                kwargs["channel"] = channel_id
                if "text" not in kwargs:
                    kwargs["text"] = "Response"
                await client.chat_postMessage(**kwargs)

            try:
                # Route directly to service-manager MCP
                await self._process_svc_command(
                    text=text,
                    user_id=user_id,
                    channel_id=channel_id,
                    client=client,
                    respond=public_respond,
                )
            except Exception as e:
                logger.exception(f"Error handling /svc command: {e}")
                await client.chat_postMessage(channel=channel_id, text=f"Error: {str(e)}")

        @self.app.command("/awx")
        async def handle_awx_command(ack, command, client, respond):
            """Handle /awx slash command for AWX playbook operations."""
            await ack()
            text = command.get("text", "").strip()
            channel_id = command.get("channel_id")
            user_id = command.get("user_id")
            logger.info(f"Received /awx command: '{text}' from user {user_id}")

            # Create a public responder that posts to channel
            async def public_respond(**kwargs):
                kwargs["channel"] = channel_id
                if "text" not in kwargs:
                    kwargs["text"] = "Response"
                await client.chat_postMessage(**kwargs)

            try:
                # Route to awx-playbook MCP by prefixing with playbook context
                await self._process_message(
                    text=text if text else "list playbooks",
                    user_id=user_id,
                    channel_id=channel_id,
                    client=client,
                    respond=public_respond,
                )
            except Exception as e:
                logger.exception(f"Error handling /awx command: {e}")
                await client.chat_postMessage(channel=channel_id, text=f"Error: {str(e)}")

        @self.app.command("/tf")
        async def handle_tf_command(ack, command, client, respond):
            """Handle /tf slash command for Terraform resource scaling."""
            await ack()
            text = command.get("text", "").strip()
            channel_id = command.get("channel_id")
            user_id = command.get("user_id")
            user_name = command.get("user_name", user_id)
            logger.info(f"Received /tf command: '{text}' from user {user_name} ({user_id})")

            async def public_respond(**kwargs):
                kwargs["channel"] = channel_id
                if "text" not in kwargs:
                    kwargs["text"] = "Response"
                await client.chat_postMessage(**kwargs)

            try:
                await self._process_tf_command(
                    text=text,
                    user_id=user_id,
                    user_name=user_name,
                    channel_id=channel_id,
                    client=client,
                    respond=public_respond,
                )
            except Exception as e:
                logger.exception(f"Error handling /tf command: {e}")
                await client.chat_postMessage(channel=channel_id, text=f"Error: {str(e)}")

        @self.app.command("/pods")
        async def handle_pods_command(ack, command, client, respond):
            """Handle /pods slash command for Kubernetes pod monitoring."""
            await ack()
            text = command.get("text", "").strip()
            channel_id = command.get("channel_id")
            user_id = command.get("user_id")
            logger.info(f"Received /pods command: '{text}' from user {user_id}")

            # Create a public responder that posts to channel
            async def public_respond(**kwargs):
                kwargs["channel"] = channel_id
                if "text" not in kwargs:
                    kwargs["text"] = "Response"
                await client.chat_postMessage(**kwargs)

            try:
                await self._process_pods_command(
                    text=text,
                    user_id=user_id,
                    channel_id=channel_id,
                    client=client,
                    respond=public_respond,
                )
            except Exception as e:
                logger.exception(f"Error handling /pods command: {e}")
                await client.chat_postMessage(channel=channel_id, text=f"Error: {str(e)}")

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

        @self.app.action(re.compile(r"^confirm_.*"))
        async def handle_confirm(ack, action, body, client, respond):
            """Handle confirmation button clicks - silently ignore old buttons."""
            await ack()
            # Don't respond at all - just silently acknowledge
            # This prevents confusing "expired" messages from appearing
            logger.debug(f"Ignored old confirm button: {action.get('value')}")

        @self.app.action(re.compile(r"^cancel_.*"))
        async def handle_cancel(ack, action, body, client, respond):
            """Handle cancel button clicks - silently ignore old buttons."""
            await ack()
            # Don't respond at all - just silently acknowledge
            logger.debug(f"Ignored old cancel button: {action.get('value')}")

        # ---- Restart confirmation button handlers ----

        @self.app.action(re.compile(r"svc_confirm_.*"))
        async def handle_svc_confirm(ack, action, body, client):
            """Handle Step 1 Yes button for restart confirmation."""
            await ack()
            await self._handle_restart_step1_yes(action, body, client)

        @self.app.action(re.compile(r"svc_cancel_.*"))
        async def handle_svc_cancel(ack, action, body, client):
            """Handle Step 1 Cancel button for restart confirmation."""
            await ack()
            await self._handle_restart_cancel(action, body, client)

        @self.app.action(re.compile(r"svc_notify_restart_.*"))
        async def handle_svc_notify_restart(ack, action, body, client):
            """Handle Step 2 'Yes, notify & restart' button."""
            await ack()
            await self._handle_restart_execute(action, body, client, notify=True)

        @self.app.action(re.compile(r"svc_restart_only_.*"))
        async def handle_svc_restart_only(ack, action, body, client):
            """Handle Step 2 'Restart without notification' button."""
            await ack()
            await self._handle_restart_execute(action, body, client, notify=False)

        @self.app.action(re.compile(r"svc_cancel2_.*"))
        async def handle_svc_cancel2(ack, action, body, client):
            """Handle Step 2 Cancel button for restart confirmation."""
            await ack()
            await self._handle_restart_cancel(action, body, client)

        # ---- Pod alert button handlers ----

        @self.app.action(re.compile(r"alert_resolve_.*"))
        async def handle_alert_resolve(ack, action, body, client):
            """Handle Resolve button on pod alerts."""
            await ack()
            await self._handle_pod_alert_action(action, body, client, "resolve")

        @self.app.action(re.compile(r"alert_pause_1d_.*"))
        async def handle_alert_pause_1d(ack, action, body, client):
            """Handle Pause 1d button on pod alerts."""
            await ack()
            await self._handle_pod_alert_action(action, body, client, "pause", hours=24)

        @self.app.action(re.compile(r"alert_pause_1w_.*"))
        async def handle_alert_pause_1w(ack, action, body, client):
            """Handle Pause 1w button on pod alerts."""
            await ack()
            await self._handle_pod_alert_action(action, body, client, "pause", hours=168)

        @self.app.action(re.compile(r"alert_selfheal_.*"))
        async def handle_alert_selfheal(ack, action, body, client):
            """Handle Self-Resolve (AI) button on pod alerts."""
            await ack()
            channel_id = body.get("channel", {}).get("id", "")
            message_ts = body.get("message", {}).get("ts", "")
            if channel_id and message_ts:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=":robot_face: Self-heal is not yet implemented. Use *Resolve* or *Pause* for now.",
                    thread_ts=message_ts,
                )

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

    async def _process_svc_command(
        self,
        text: str,
        user_id: str,
        channel_id: str,
        client: AsyncWebClient,
        respond,
    ):
        """Process /svc command - route directly to service-manager MCP."""
        import re

        if not text.strip():
            await respond(text=self._get_svc_help_message())
            return

        text_lower = text.lower().strip()

        # Handle help
        if text_lower in ["help", "?", "list", "roles"]:
            # Route to list-service-roles action
            result = await self.registry.route_action(
                mcp_name="service-manager",
                action="list-service-roles",
                parameters={},
                user_id=user_id,
                channel_id=channel_id,
            )
            msg = result.to_slack_message()
            await respond(**msg)
            return

        # Parse the command: <action> <role> in/on <domain>
        words = re.findall(r'[\w-]+', text_lower)

        # Known service roles
        service_roles = {'mim', 'mphpp', 'mphhos', 'ts', 'www', 'www5', 'ngx', 'ngxint',
                        'redis', 'mongodb', 'tps', 'harjo', 'hamim', 'haweb', 'srouter',
                        'sdecoder', 'scapture', 'ser', 'sconductor', 'mimmem', 'provnstatdb5'}

        # Detect action
        action = None
        if any(w in words for w in ['version', 'versions', 'sf', 'whats', "what's"]):
            action = 'get-version'
        elif any(w in words for w in ['check', 'status']):
            action = 'check-service'
        elif 'restart' in words:
            action = 'restart-service'
        elif 'start' in words and 'restart' not in text_lower:
            action = 'start-service'
        elif 'stop' in words:
            action = 'stop-service'
        elif any(w in words for w in ['logs', 'log', 'journal']):
            action = 'service-logs'
        else:
            # Default to check-service if no action keyword
            action = 'check-service'

        # Extract role, domain, and optional host filter
        skip_words = {'check', 'status', 'restart', 'start', 'stop', 'logs', 'log',
                     'journal', 'in', 'on', 'for', 'the', 'service', 'host', 'only',
                     'version', 'versions', 'sf', 'whats', "what's", 'software'}
        potential_terms = [w for w in words if w not in skip_words and len(w) >= 2]

        role = None
        domain = None
        host_filter = None

        # Check for "host <name>" pattern
        if 'host' in words:
            host_idx = words.index('host')
            if host_idx + 1 < len(words):
                host_filter = words[host_idx + 1]

        for term in potential_terms:
            if term in service_roles and not role:
                role = term
            elif role and not domain and term != host_filter:
                domain = term

        if not role or not domain:
            await respond(text=f":warning: Could not parse command: `{text}`\n\n{self._get_svc_help_message()}")
            return

        # Confirmation for all restarts
        if action == 'restart-service':
            two_step = role in RESTART_CONFIRM_ROLES
            await self._initiate_restart_confirmation(
                role=role,
                domain=domain,
                host_filter=host_filter,
                user_id=user_id,
                channel_id=channel_id,
                client=client,
                two_step=two_step,
            )
            return

        # Post initial status message
        host_info = f" (host: {host_filter})" if host_filter else ""
        initial_message = f"<@{user_id}> 🔍 Checking `{role}` service in `{domain}`{host_info}..."
        msg_response = await client.chat_postMessage(
            channel=channel_id,
            text=initial_message,
            mrkdwn=True,
        )

        # Build parameters
        params = {"role": role, "domain": domain}
        if host_filter:
            params["host"] = host_filter

        # Execute the action
        result = await self.registry.route_action(
            mcp_name="service-manager",
            action=action,
            parameters=params,
            user_id=user_id,
            channel_id=channel_id,
        )

        # Send result
        msg = result.to_slack_message()
        await respond(**msg)

    def _get_svc_help_message(self) -> str:
        """Generate help message for /svc command."""
        return """*Service Manager (/svc)*

Check and manage services on servers via direct SSH.

*Usage:*
- `/svc check <role> in <domain>` - Check service status on all hosts
- `/svc check <role> in <domain> host <name>` - Check specific host only
- `/svc restart <role> on <domain>` - Restart service
- `/svc start <role> on <domain>` - Start service
- `/svc stop <role> on <domain>` - Stop service
- `/svc logs <role> in <domain>` - Get service logs
- `/svc logs <role> in <domain> host <name>` - Get logs from specific host
- `/svc version <role> in <domain>` - Get software version
- `/svc list` or `/svc roles` - List supported roles

*Examples:*
- `/svc check mim in lionamxp` - Check all mim hosts
- `/svc check mim in lionamxp host mim5` - Check only mim5
- `/svc logs mim in aptus2 host mim3` - Get logs from mim3 only
- `/svc version mim in hyxd` - Get mongooseim version
- `/svc version mphpp in pubgxp` - Get morpheus version
- `/svc restart mim on hyxd`

*Supported Roles:*
`mim`, `mphpp`, `mphhos`, `ts`, `www5`, `ngx`, `ngxint`, `redis`, `mongodb`, `tps`, `harjo`, `hamim`, `haweb`, `srouter`, `sdecoder`, `scapture`, `ser`, `sconductor`
"""

    # ---- Restart confirmation flow ----

    async def _initiate_restart_confirmation(
        self,
        role: str,
        domain: str,
        host_filter: Optional[str],
        user_id: str,
        channel_id: str,
        client: AsyncWebClient,
        two_step: bool = False,
    ):
        """Post confirmation prompt. two_step=True adds a notification choice (critical roles)."""
        self._cleanup_stale_restarts()

        service_name = ROLE_DISPLAY_NAMES.get(role, role)

        # Count hosts via the etcd-awx-sync MCP
        host_count = None
        try:
            count_result = await self.registry.route_action(
                mcp_name="etcd-awx-sync",
                action="count",
                parameters={"domain": domain, "role": role},
                user_id=user_id,
                channel_id=channel_id,
            )
            if count_result.data and "count" in count_result.data:
                host_count = count_result.data["count"]
        except Exception as e:
            logger.warning(f"Failed to count hosts for restart confirmation: {e}")

        if host_filter:
            host_desc = f"host `{host_filter}` in `{domain}`"
        elif host_count is not None:
            host_desc = f"*{host_count} host{'s' if host_count != 1 else ''}* in `{domain}`"
        else:
            host_desc = f"matching hosts in `{domain}`"

        restart_id = uuid.uuid4().hex[:8]
        self._pending_restarts[restart_id] = {
            "role": role,
            "domain": domain,
            "host_filter": host_filter,
            "host_count": host_count,
            "service_name": service_name,
            "user_id": user_id,
            "channel_id": channel_id,
            "message_ts": None,  # set after posting
            "created_at": time.time(),
            "two_step": two_step,
        }

        # For two_step (critical roles): Yes goes to step 2 (notification choice)
        # For single-step (other roles): Yes goes straight to restart
        yes_action_id = f"svc_confirm_{restart_id}" if two_step else f"svc_restart_only_{restart_id}"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":warning: <@{user_id}> Are you sure you want to restart "
                        f"*{service_name}* on {host_desc}?"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Yes"},
                        "style": "danger",
                        "action_id": yes_action_id,
                        "value": restart_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "action_id": f"svc_cancel_{restart_id}",
                        "value": restart_id,
                    },
                ],
            },
        ]

        msg_response = await client.chat_postMessage(
            channel=channel_id,
            text=f"Restart confirmation for {service_name} in {domain}",
            blocks=blocks,
        )
        self._pending_restarts[restart_id]["message_ts"] = msg_response.get("ts")

    async def _handle_restart_step1_yes(self, action: dict, body: dict, client: AsyncWebClient):
        """Step 1 Yes: show Step 2 notification choice."""
        restart_id = action.get("value", "")
        clicking_user = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = body.get("message", {}).get("ts", "")

        state = self._pending_restarts.get(restart_id)
        if not state:
            logger.debug(f"Ignored stale restart confirm button: {restart_id}")
            return

        # Only the original requester can confirm
        if clicking_user != state["user_id"]:
            return

        service_name = state["service_name"]
        domain = state["domain"]
        host_filter = state.get("host_filter")
        host_desc = f"host `{host_filter}` in `{domain}`" if host_filter else f"`{domain}`"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":rotating_light: You are going to create disruption in service by restarting "
                        f"*{service_name}* on {host_desc}.\n"
                        f"Would you like to add a notification on <#"
                        f"{os.environ.get('OPS_NOTIFICATION_CHANNEL_ID', 'C01LM1U6YG7')}>?"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Yes, notify & restart"},
                        "style": "primary",
                        "action_id": f"svc_notify_restart_{restart_id}",
                        "value": restart_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Restart without notification"},
                        "style": "danger",
                        "action_id": f"svc_restart_only_{restart_id}",
                        "value": restart_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "action_id": f"svc_cancel2_{restart_id}",
                        "value": restart_id,
                    },
                ],
            },
        ]

        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"Restart notification choice for {service_name} in {domain}",
            blocks=blocks,
        )

    async def _handle_restart_cancel(self, action: dict, body: dict, client: AsyncWebClient):
        """Cancel handler for both Step 1 and Step 2."""
        restart_id = action.get("value", "")
        clicking_user = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = body.get("message", {}).get("ts", "")

        state = self._pending_restarts.pop(restart_id, None)
        if not state:
            logger.debug(f"Ignored stale restart cancel button: {restart_id}")
            return

        # Only the original requester can cancel
        if clicking_user != state["user_id"]:
            self._pending_restarts[restart_id] = state  # put it back
            return

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":no_entry_sign: Restart of *{state['service_name']}* in `{state['domain']}` cancelled by <@{clicking_user}>.",
                },
            },
        ]
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"Restart cancelled for {state['service_name']} in {state['domain']}",
            blocks=blocks,
        )

    async def _handle_restart_execute(self, action: dict, body: dict, client: AsyncWebClient, notify: bool):
        """Execute the restart, optionally posting a notification first."""
        restart_id = action.get("value", "")
        clicking_user = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = body.get("message", {}).get("ts", "")

        state = self._pending_restarts.pop(restart_id, None)
        if not state:
            logger.debug(f"Ignored stale restart execute button: {restart_id}")
            return

        # Only the original requester can execute
        if clicking_user != state["user_id"]:
            self._pending_restarts[restart_id] = state  # put it back
            return

        role = state["role"]
        domain = state["domain"]
        host_filter = state.get("host_filter")
        service_name = state["service_name"]
        host_desc = f"host `{host_filter}` in `{domain}`" if host_filter else f"`{domain}`"

        # Update message to show in-progress
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":hourglass_flowing_sand: Restarting *{service_name}* on {host_desc}...",
                },
            },
        ]
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"Restarting {service_name} in {domain}...",
            blocks=blocks,
        )

        # Post notification if requested
        if notify:
            notification_channel = os.environ.get("OPS_NOTIFICATION_CHANNEL_ID", "C01LM1U6YG7")
            try:
                await client.chat_postMessage(
                    channel=notification_channel,
                    text=(
                        f":rotating_light: *Service Restart* — <@{clicking_user}> is restarting "
                        f"*{service_name}* on {host_desc}."
                    ),
                    mrkdwn=True,
                )
            except Exception as e:
                logger.error(f"Failed to post restart notification: {e}")

        # Execute the restart
        params = {"role": role, "domain": domain}
        if host_filter:
            params["host"] = host_filter

        try:
            result = await self.registry.route_action(
                mcp_name="service-manager",
                action="restart-service",
                parameters=params,
                user_id=clicking_user,
                channel_id=channel_id,
            )

            # Update message with result
            status_emoji = ":white_check_mark:" if result.status.value == "success" else ":x:"
            notify_note = " (notified <#" + os.environ.get("OPS_NOTIFICATION_CHANNEL_ID", "C01LM1U6YG7") + ">)" if notify else ""
            result_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{status_emoji} Restart of *{service_name}* on {host_desc} "
                            f"by <@{clicking_user}>{notify_note}\n\n{result.message}"
                        ),
                    },
                },
            ]
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Restart result for {service_name} in {domain}",
                blocks=result_blocks,
            )
        except Exception as e:
            logger.exception(f"Failed to execute restart: {e}")
            error_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":x: Restart of *{service_name}* on {host_desc} failed: {e}",
                    },
                },
            ]
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Restart failed for {service_name} in {domain}",
                blocks=error_blocks,
            )

    def _cleanup_stale_restarts(self):
        """Remove pending restarts older than 10 minutes."""
        cutoff = time.time() - 600
        stale_ids = [rid for rid, state in self._pending_restarts.items() if state["created_at"] < cutoff]
        for rid in stale_ids:
            del self._pending_restarts[rid]
        if stale_ids:
            logger.debug(f"Cleaned up {len(stale_ids)} stale restart confirmation(s)")

    async def _process_tf_command(
        self,
        text: str,
        user_id: str,
        user_name: str,
        channel_id: str,
        client: AsyncWebClient,
        respond,
    ):
        """Process /tf command - route directly to tf-manager MCP."""
        import re

        if not text.strip():
            await respond(text=self._get_tf_help_message())
            return

        text_lower = text.lower().strip()
        words = text_lower.split()
        # Keep original-case words for run IDs (case-sensitive)
        original_words = text.strip().split()

        # Handle help
        if text_lower in ["help", "?"]:
            result = await self.registry.route_action(
                mcp_name="tf-manager",
                action="show-help",
                parameters={},
                user_id=user_id,
                channel_id=channel_id,
            )
            msg = result.to_slack_message()
            await respond(**msg)
            return

        # Handle status
        if text_lower in ["status", "pending"]:
            await client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> :mag: Checking pending TF operations...",
            )
            result = await self.registry.route_action(
                mcp_name="tf-manager",
                action="show-status",
                parameters={},
                user_id=user_id,
                channel_id=channel_id,
            )
            msg = result.to_slack_message()
            await respond(**msg)
            return

        # Handle show <domain>
        if words[0] == "show" and len(words) >= 2:
            domain = words[1]
            await client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> :mag: Fetching resource counts for `{domain}`...",
            )
            result = await self.registry.route_action(
                mcp_name="tf-manager",
                action="show-domain",
                parameters={"domain": domain},
                user_id=user_id,
                channel_id=channel_id,
            )
            msg = result.to_slack_message()
            await respond(**msg)
            return

        # Handle confirm <run_id> — use original case for run ID
        if words[0] == "confirm" and len(words) >= 2:
            run_id = original_words[1]
            await client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> :rocket: Applying TF run `{run_id}`... (this may take several minutes)",
            )
            result = await self.registry.route_action(
                mcp_name="tf-manager",
                action="confirm-apply",
                parameters={"run_id": run_id, "user_name": user_name},
                user_id=user_id,
                channel_id=channel_id,
            )
            msg = result.to_slack_message()
            await respond(**msg)
            return

        # Handle cancel <run_id>
        if words[0] == "cancel" and len(words) >= 2:
            run_id = original_words[1]
            result = await self.registry.route_action(
                mcp_name="tf-manager",
                action="cancel-run",
                parameters={"run_id": run_id, "user_name": user_name},
                user_id=user_id,
                channel_id=channel_id,
            )
            msg = result.to_slack_message()
            await respond(**msg)
            return

        # Handle scale commands: add/remove/set
        # Patterns:
        #   add <count> <role> to <domain>
        #   add <count> <role> <region> to <domain>
        #   remove <count> <role> from <domain>
        #   set <role> <count> in <domain>
        action = None
        params = {}

        if words[0] in ("add", "remove") and len(words) >= 4:
            action = "scale-resource"
            operation = words[0]
            try:
                count = int(words[1])
            except ValueError:
                await respond(text=f":warning: Invalid count: `{words[1]}`\n\nUsage: `/tf {operation} <count> <role> {'to' if operation == 'add' else 'from'} <domain>`")
                return

            role = words[2]

            # Check for --confirm flag
            confirmed = "--confirm" in text_lower

            # Find domain (after "to" or "from")
            domain = None
            region = None
            for i, w in enumerate(words[3:], start=3):
                if w in ("to", "from", "in") and i + 1 < len(words):
                    domain = words[i + 1].replace("--confirm", "").strip()
                    break
                elif w not in ("to", "from", "in", "--confirm"):
                    # Could be a region for mphpp
                    region = w

            if not domain:
                await respond(text=f":warning: Could not find domain in command.\n\nUsage: `/tf {operation} {count} {role} {'to' if operation == 'add' else 'from'} <domain>`")
                return

            params = {
                "role": role,
                "domain": domain,
                "operation": operation,
                "count": count,
            }
            if region:
                params["region"] = region
            if confirmed:
                params["_confirmed"] = True

        elif words[0] == "set" and len(words) >= 4:
            action = "scale-resource"
            role = words[1]
            try:
                count = int(words[2])
            except ValueError:
                await respond(text=f":warning: Invalid count: `{words[2]}`\n\nUsage: `/tf set <role> <count> in <domain>`")
                return

            # Find domain (after "in")
            domain = None
            for i, w in enumerate(words[3:], start=3):
                if w == "in" and i + 1 < len(words):
                    domain = words[i + 1]
                    break

            if not domain:
                # Maybe: /tf set <role> <count> <domain>
                domain = words[3] if len(words) > 3 else None

            if not domain:
                await respond(text=":warning: Could not find domain.\n\nUsage: `/tf set <role> <count> in <domain>`")
                return

            confirmed = "--confirm" in text_lower
            params = {
                "role": role,
                "domain": domain,
                "operation": "set",
                "count": count,
            }
            if confirmed:
                params["_confirmed"] = True

        else:
            await respond(text=f":warning: Could not parse command: `{text}`\n\n{self._get_tf_help_message()}")
            return

        # Post initial status
        op_desc = f"{params['operation']} {params.get('count', '')} {params['role']}"
        await client.chat_postMessage(
            channel=channel_id,
            text=f"<@{user_id}> :gear: Scaling `{params['role']}` in `{params['domain']}` ({op_desc})... this may take several minutes.",
        )

        # Execute
        result = await self.registry.route_action(
            mcp_name="tf-manager",
            action=action,
            parameters=params,
            user_id=user_id,
            channel_id=channel_id,
        )

        msg = result.to_slack_message()
        await respond(**msg)

    def _get_tf_help_message(self) -> str:
        """Generate help message for /tf command."""
        return """*Terraform Manager (/tf)*

Scale OpenStack resources managed by Terraform.

*Scaling:*
- `/tf add <count> <role> to <domain>` - Increase instance count
- `/tf remove <count> <role> from <domain>` - Decrease instance count
- `/tf set <role> <count> in <domain>` - Set exact instance count

*mphpp (region-specific):*
- `/tf add 2 mphpp bos_2 to aptus2` - Specify region
- `/tf add 2 mphpp to aptus2` - Auto-detect if only one region

*View:*
- `/tf show <domain>` - Show current resource counts
- `/tf status` - Show pending operations

*Confirm/Cancel:*
- `/tf confirm <run_id>` - Apply a pending plan
- `/tf cancel <run_id>` - Discard a pending plan

*Examples:*
- `/tf add 2 mim to aptus2`
- `/tf remove 1 ts from lionxp`
- `/tf set mim 5 in pubwxp`
- `/tf show aptus2`
"""

    async def _process_pods_command(
        self,
        text: str,
        user_id: str,
        channel_id: str,
        client: AsyncWebClient,
        respond,
    ):
        """Process /pods command - route directly to pod-monitor MCP."""
        if not text.strip():
            text = "list"

        text_lower = text.lower().strip()
        words = text_lower.split()

        # Handle help
        if text_lower in ["help", "?"]:
            await respond(text=self._get_pods_help_message())
            return

        # Parse the command
        action = None
        params = {}

        if words[0] in ["list", "ls", "status", "check"]:
            action = "list-pods"
            if len(words) > 1:
                params["namespace"] = words[1]

        elif words[0] in ["details", "detail", "describe", "info"]:
            action = "pod-details"
            if len(words) > 1:
                params["pod"] = words[1]
            if len(words) > 2:
                params["namespace"] = words[2]

        elif words[0] in ["logs", "log"]:
            action = "pod-logs"
            if len(words) > 1:
                params["pod"] = words[1]
            if len(words) > 2:
                # Check if second arg is a number (lines) or namespace
                try:
                    params["lines"] = int(words[2])
                except ValueError:
                    params["namespace"] = words[2]
            if len(words) > 3:
                params["namespace"] = words[3]

        elif words[0] in ["unhealthy", "failing", "broken", "bad"]:
            action = "unhealthy-pods"
            if len(words) > 1:
                params["namespace"] = words[1]

        elif words[0] in ["summary", "overview", "stats"]:
            action = "namespace-summary"
            if len(words) > 1:
                params["namespace"] = words[1]

        else:
            # Default: treat as pod name for details, or list if short
            if len(words[0]) <= 3:
                action = "list-pods"
                params["namespace"] = words[0]
            else:
                action = "pod-details"
                params["pod"] = words[0]
                if len(words) > 1:
                    params["namespace"] = words[1]

        # Post initial status message
        initial_message = f"<@{user_id}> :mag: Checking pods..."
        await client.chat_postMessage(
            channel=channel_id,
            text=initial_message,
            mrkdwn=True,
        )

        # Execute the action
        result = await self.registry.route_action(
            mcp_name="pod-monitor",
            action=action,
            parameters=params,
            user_id=user_id,
            channel_id=channel_id,
        )

        # Send result
        msg = result.to_slack_message()
        await respond(**msg)

    async def _handle_pod_alert_action(
        self,
        action: dict,
        body: dict,
        client: AsyncWebClient,
        action_type: str,
        hours: int = 0,
    ):
        """Handle a pod alert button click (resolve or pause)."""
        import aiohttp

        value = action.get("value", "")
        user_id = body.get("user", {}).get("id", "unknown")
        channel_id = body.get("channel", {}).get("id", "")
        message_ts = body.get("message", {}).get("ts", "")

        # Value is "pod_name|issue"
        if "|" not in value:
            logger.warning(f"Invalid alert button value: {value}")
            return

        pod_name, issue = value.split("|", 1)

        pod_monitor_url = os.environ.get("POD_MONITOR_URL", "http://pod-monitor:8082")

        try:
            async with aiohttp.ClientSession() as session:
                if action_type == "resolve":
                    async with session.post(
                        f"{pod_monitor_url}/alert/resolve",
                        json={"pod": pod_name, "issue": issue, "user_id": user_id},
                    ) as resp:
                        result = await resp.json()
                        logger.info(f"Alert resolve response: {result}")

                    # Update the original message
                    if channel_id and message_ts:
                        resolved_blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f":white_check_mark: ~*Pod Alert: `{pod_name}`*~\n"
                                        f"*Issue:* {issue}\n"
                                        f"*Resolved by:* <@{user_id}>"
                                    ),
                                },
                            },
                        ]
                        await client.chat_update(
                            channel=channel_id,
                            ts=message_ts,
                            text=f"Resolved: {pod_name} — {issue}",
                            blocks=resolved_blocks,
                        )

                elif action_type == "pause":
                    async with session.post(
                        f"{pod_monitor_url}/alert/pause",
                        json={"pod": pod_name, "issue": issue, "hours": hours, "user_id": user_id},
                    ) as resp:
                        result = await resp.json()
                        logger.info(f"Alert pause response: {result}")

                    # Update the original message
                    if channel_id and message_ts:
                        duration_str = f"{hours // 24} day(s)" if hours >= 24 else f"{hours}h"
                        paused_blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f":pause_button: *Pod Alert: `{pod_name}`* (paused)\n"
                                        f"*Issue:* {issue}\n"
                                        f"*Paused for:* {duration_str} by <@{user_id}>"
                                    ),
                                },
                            },
                        ]
                        await client.chat_update(
                            channel=channel_id,
                            ts=message_ts,
                            text=f"Paused: {pod_name} — {issue}",
                            blocks=paused_blocks,
                        )

        except Exception as e:
            logger.error(f"Failed to handle alert {action_type} for {pod_name}: {e}")
            if channel_id and message_ts:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f":warning: Failed to {action_type} alert: {e}",
                    thread_ts=message_ts,
                )

    def _get_pods_help_message(self) -> str:
        """Generate help message for /pods command."""
        return """*Pod Monitor (/pods)*

Monitor Kubernetes pod health, status, logs, and resources.

*Usage:*
- `/pods` or `/pods list` - List all pods (default namespace)
- `/pods list <namespace>` - List pods in a namespace
- `/pods details <pod-name>` - Detailed pod info (events, resources, images)
- `/pods logs <pod-name>` - Last 100 log lines
- `/pods logs <pod-name> <lines>` - Last N log lines
- `/pods unhealthy` - Show only failing/unhealthy pods
- `/pods summary` - Namespace overview (counts, health, resources)
- `/pods help` - Show this message

*Examples:*
- `/pods` - List all pods in default namespace
- `/pods list kube-system` - List pods in kube-system
- `/pods details slack-mcp-agent` - Show details for a pod (fuzzy match)
- `/pods logs service-manager 50` - Last 50 log lines
- `/pods unhealthy` - Show CrashLoopBackOff, OOMKilled, etc.
- `/pods summary` - Namespace overview

_Pod names support fuzzy matching - e.g., "slack-mcp" matches "slack-mcp-agent-abc123"_
"""

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
            mcp_context = self.registry.get_llm_context()
            intent = await self.llm_client.parse_intent(
                user_message=text,
                mcp_context=mcp_context,
            )

            logger.info(f"Parsed intent: mcp={intent.mcp_name}, action={intent.action}, "
                       f"params={intent.parameters}, confidence={intent.confidence}")

            # Build parsed info for display
            parsed_info = []
            if intent.parameters.get("role"):
                parsed_info.append(f"Role: `{intent.parameters['role']}`")
            if intent.parameters.get("domain"):
                parsed_info.append(f"Domain: `{intent.parameters['domain']}`")
            parsed_str = " | ".join(parsed_info) if parsed_info else "_no filters_"

            # Handle unknown intent
            if intent.mcp_name == "unknown" or intent.confidence < 0.5:
                await respond(
                    text=f"*Query:* `{text}`\n"
                         f"*Parsed:* {parsed_str}\n\n"
                         f"I'm not sure what you want to do. {intent.explanation}\n\n"
                         f"Try asking for `help` or be more specific."
                )
                return

            # Generate a short task ID for tracking
            import random
            import string
            task_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))

            # Post initial message and capture timestamp for threading
            initial_message = (
                f"<@{user_id}> 📋 *Task `{task_id}`*\n"
                f"*Query:* `{text}`\n"
                f"*Action:* `{intent.action}` | {parsed_str}\n"
                f"⏳ _Processing... (timeout: 5 min)_"
            )

            # Post with chat_postMessage to capture the timestamp
            msg_response = await client.chat_postMessage(
                channel=channel_id,
                text=initial_message,
                mrkdwn=True,
            )
            message_ts = msg_response.get("ts")

            # Route to MCP with message_ts for threading
            result = await self.registry.route_action(
                mcp_name=intent.mcp_name,
                action=intent.action,
                parameters=intent.parameters,
                user_id=user_id,
                channel_id=channel_id,
                message_ts=message_ts,
            )

            await self._send_result(result, respond, client, channel_id, message_ts)

        except Exception as e:
            logger.exception("Error processing message")
            await respond(text=f"Error: {str(e)}")

    async def _send_result(
        self,
        result: MCPResult,
        respond,
        client: AsyncWebClient,
        channel_id: str,
        message_ts: Optional[str] = None,
    ):
        """Send an MCP result back to Slack."""
        logger.info(f"Sending result: status={result.status}, action_id={result.action_id}")

        if result.status == MCPResultStatus.NEEDS_CONFIRMATION:
            # Send confirmation prompt with buttons
            logger.info(f"Sending confirmation with action_id={result.action_id}")
            if result.blocks:
                await respond(text=result.message, blocks=result.blocks)
            else:
                await respond(text=result.confirmation_prompt or result.message)
        else:
            # Handle threaded responses if available
            if message_ts and (result.thread_messages or result.main_message_update):
                # Post thread messages first
                if result.thread_messages:
                    for thread_msg in result.thread_messages:
                        try:
                            await client.chat_postMessage(
                                channel=channel_id,
                                text=thread_msg,
                                thread_ts=message_ts,
                                mrkdwn=True,
                            )
                        except Exception as e:
                            logger.error(f"Failed to post thread message: {e}")

                # Update main message with result
                if result.main_message_update:
                    try:
                        # Get current message
                        history = await client.conversations_history(
                            channel=channel_id,
                            latest=message_ts,
                            inclusive=True,
                            limit=1,
                        )
                        if history["messages"]:
                            current_text = history["messages"][0].get("text", "")
                            # Build the update - remove the processing line and add result
                            lines = current_text.split("\n")
                            # Remove the "Processing..." line
                            lines = [l for l in lines if "Processing" not in l and "⏳" not in l]
                            # Add result line
                            result_line = result.main_message_update
                            if result.awx_url:
                                result_line += f" | <{result.awx_url}|View in AWX>"
                            lines.append(result_line)
                            new_text = "\n".join(lines)

                            await client.chat_update(
                                channel=channel_id,
                                ts=message_ts,
                                text=new_text,
                                mrkdwn=True,
                            )
                    except Exception as e:
                        logger.error(f"Failed to update main message: {e}")
                        # Fallback: post as new message
                        await respond(text=result.message)
            else:
                # Send regular message (legacy behavior)
                msg = result.to_slack_message()
                await respond(**msg)

    def _get_help_message(self) -> str:
        """Generate help message."""
        mcp_list = self.registry.get_mcp_list_for_llm()

        return f"""*Slack MCP Agent*

I'm an AI-powered assistant that can help you manage infrastructure operations.

*Slash Commands:*
- `/awx <request>` - AWX playbook & inventory operations
- `/svc <request>` - Service management via SSH
- `/tf <request>` - Terraform resource scaling (OpenStack)
- `/pods <request>` - Kubernetes pod monitoring
- `/agent <request>` - General requests (routes to appropriate tool)

*AWX Commands (/awx):*
- `/awx list playbooks` - Show available playbooks
- `/awx run <playbook> on <inventory>` - Execute a playbook
- `/awx run <playbook> globally` - Run on all hosts (central inventory)
- `/awx job status <id>` - Check job status
- `/awx list jobs` - Show recent jobs
- `/awx queue status` - See running/queued jobs
- `/awx sync all` - Sync all inventory from etcd

*Service Commands (/svc):*
- `/svc check <role> in <domain>` - Check service status
- `/svc restart <role> on <domain>` - Restart service
- `/svc logs <role> in <domain>` - Get service logs
- `/svc list` - List supported service roles

*Terraform Commands (/tf):*
- `/tf add <count> <role> to <domain>` - Scale up
- `/tf remove <count> <role> from <domain>` - Scale down
- `/tf set <role> <count> in <domain>` - Set exact count
- `/tf show <domain>` - Show current counts
- `/tf status` - Pending operations
- `/tf confirm <run_id>` - Apply a plan
- `/tf cancel <run_id>` - Discard a plan

*Pod Commands (/pods):*
- `/pods` - List all pods in default namespace
- `/pods details <pod-name>` - Detailed pod info
- `/pods logs <pod-name>` - Get pod logs
- `/pods unhealthy` - Show failing pods
- `/pods summary` - Namespace overview

*Examples:*
- `/awx run check_mim.yml on mim-lionamxp`
- `/svc check mim in lionamxp`
- `/svc restart ngx on pubwxp`
- `/tf add 2 mim to aptus2`
- `/tf show aptus2`
- `/pods details slack-mcp-agent`
- `/pods unhealthy`

{mcp_list}

Need more help? Just ask!
"""

    async def _notify_channel(
        self,
        channel_id: str,
        message: str,
        thread_ts: Optional[str] = None,
        update_ts: Optional[str] = None,
    ):
        """
        Send a notification message to a channel (used by queue).

        Args:
            channel_id: Slack channel ID
            message: Message text
            thread_ts: If provided, post as reply in thread
            update_ts: If provided, update the existing message instead of posting new
        """
        try:
            client = self.app.client

            if update_ts:
                # Update existing message (append result to main message)
                try:
                    # Get the current message first
                    result = await client.conversations_history(
                        channel=channel_id,
                        latest=update_ts,
                        inclusive=True,
                        limit=1,
                    )
                    if result["messages"]:
                        current_text = result["messages"][0].get("text", "")
                        # Append the new result line
                        new_text = f"{current_text}\n{message}"
                        await client.chat_update(
                            channel=channel_id,
                            ts=update_ts,
                            text=new_text,
                            mrkdwn=True,
                        )
                except Exception as e:
                    logger.error(f"Failed to update message, posting new: {e}")
                    # Fallback: post as new message in thread
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=message,
                        thread_ts=thread_ts or update_ts,
                        mrkdwn=True,
                    )
            elif thread_ts:
                # Post in thread
                await client.chat_postMessage(
                    channel=channel_id,
                    text=message,
                    thread_ts=thread_ts,
                    mrkdwn=True,
                )
            else:
                # Post to channel (no thread)
                await client.chat_postMessage(
                    channel=channel_id,
                    text=message,
                    mrkdwn=True,
                )
        except Exception as e:
            logger.error(f"Failed to send notification to {channel_id}: {e}")

    async def _initialize_queue(self):
        """Initialize the request queue for the AWX playbook MCP."""
        try:
            # Get the AWX playbook MCP
            awx_mcp = self.registry.get("awx-playbook")
            if awx_mcp and hasattr(awx_mcp, 'initialize_queue'):
                await awx_mcp.initialize_queue(self._notify_channel)
                logger.info("Request queue initialized for AWX playbook MCP")
        except Exception as e:
            logger.error(f"Failed to initialize queue: {e}")

    async def start(self):
        """Start the Slack agent."""
        # Initialize queue for multi-user support
        await self._initialize_queue()

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
