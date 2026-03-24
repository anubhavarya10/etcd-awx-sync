#!/usr/bin/env python3
"""
Slack Bot for etcd-awx-sync.
Provides interactive inventory management through Slack slash commands and modals.

Features:
- /inventory sync - Full sync
- /inventory create <prompt> - Smart prompt (e.g., /inventory create mphpp for pubwxp)
- /inventory list-domains - Show available domains
- /inventory list-roles - Show available roles
- /inventory help - Show usage

Requires Socket Mode for interactive features.
"""

import os
import sys
import time
import threading
from typing import Optional, Dict, Any, Tuple, Set

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    from slack_sdk.errors import SlackApiError
except ImportError:
    raise ImportError(
        "slack_bolt is required. Install with: pip install slack_bolt"
    )

# Import from local modules
from etcd_to_awx import (
    get_hosts_from_etcd,
    filter_hosts,
    parse_natural_language_prompt,
    create_awx_session,
    get_or_create_organization,
    create_inventory,
    add_hosts_to_inventory,
    create_groups_and_assign_hosts,
    check_required_env_vars,
    AWX_SERVER,
)
from slack_alerts import (
    send_sync_complete_alert,
    send_error_alert,
)


# Configuration
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")

# Validate required tokens
if not SLACK_BOT_TOKEN:
    print("Error: SLACK_BOT_TOKEN environment variable is required")
    sys.exit(1)

if not SLACK_APP_TOKEN:
    print("Error: SLACK_APP_TOKEN environment variable is required for Socket Mode")
    sys.exit(1)

# Initialize the Slack app
app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)

# Cache for discovered domains and roles
_etcd_cache: Dict[str, Any] = {
    "hosts": {},
    "domains": set(),
    "roles": set(),
    "last_refresh": 0,
}
CACHE_TTL = 300  # 5 minutes


def refresh_etcd_cache(force: bool = False) -> Tuple[Dict, Set[str], Set[str]]:
    """Refresh the etcd cache if stale or forced."""
    current_time = time.time()

    if not force and (current_time - _etcd_cache["last_refresh"]) < CACHE_TTL:
        return _etcd_cache["hosts"], _etcd_cache["domains"], _etcd_cache["roles"]

    try:
        hosts, domains, roles = get_hosts_from_etcd()
        _etcd_cache["hosts"] = hosts
        _etcd_cache["domains"] = domains
        _etcd_cache["roles"] = roles
        _etcd_cache["last_refresh"] = current_time
        return hosts, domains, roles
    except Exception as e:
        print(f"Error refreshing etcd cache: {e}")
        # Return existing cache if refresh fails
        return _etcd_cache["hosts"], _etcd_cache["domains"], _etcd_cache["roles"]


def run_sync_job(
    client,
    channel_id: str,
    thread_ts: str,
    domain_filter: Optional[str] = None,
    role_filter: Optional[str] = None,
    inventory_name: Optional[str] = None,
):
    """Run the sync job in a background thread."""
    start_time = time.time()

    try:
        # Get hosts from etcd
        all_hosts, all_domains, all_roles = get_hosts_from_etcd()

        if not all_hosts:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text="No hosts found in etcd."
            )
            return

        # Apply filters
        if domain_filter or role_filter:
            hosts = filter_hosts(all_hosts, domain_filter, role_filter)
            if not hosts:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f"No hosts match the specified filters (domain: {domain_filter or 'all'}, role: {role_filter or 'all'})."
                )
                return
        else:
            hosts = all_hosts

        # Build inventory name if not provided
        if not inventory_name:
            if role_filter and domain_filter:
                inventory_name = f"{role_filter}-{domain_filter}"
            elif role_filter:
                inventory_name = f"{role_filter}-all-domains"
            elif domain_filter:
                inventory_name = f"{domain_filter}-inventory"
            else:
                inventory_name = "central inventory"

        # Update progress
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Found {len(hosts)} hosts. Creating AWX inventory `{inventory_name}`..."
        )

        # Create AWX session and inventory
        session = create_awx_session()
        org_id = get_or_create_organization(session)
        inventory_id = create_inventory(session, org_id, inventory_name)

        # Add hosts
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Adding {len(hosts)} hosts to inventory..."
        )
        host_id_map = add_hosts_to_inventory(session, inventory_id, hosts)

        # Create groups
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Creating groups and assigning hosts..."
        )
        group_members = create_groups_and_assign_hosts(
            session, inventory_id, hosts, host_id_map
        )

        # Calculate duration
        duration = time.time() - start_time

        # Send completion alert
        send_sync_complete_alert(
            inventory_name=inventory_name,
            host_count=len(host_id_map),
            group_count=len(group_members),
            duration_seconds=duration,
            domain_filter=domain_filter,
            role_filter=role_filter,
            channel_id=channel_id,
            inventory_id=inventory_id,
            awx_server=AWX_SERVER,
        )

    except Exception as e:
        error_msg = str(e)
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Sync failed: {error_msg}"
        )
        send_error_alert(
            error_message=error_msg,
            inventory_name=inventory_name,
            context="Running sync from Slack bot",
            channel_id=channel_id,
        )


@app.command("/inventory")
def handle_inventory_command(ack, command, client, respond):
    """Handle /inventory slash command."""
    ack()

    text = command.get("text", "").strip()
    user_id = command.get("user_id")
    channel_id = command.get("channel_id")

    # Parse subcommand
    parts = text.split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    if subcommand == "help" or not subcommand:
        show_help(respond)

    elif subcommand == "sync":
        handle_full_sync(client, channel_id, user_id, respond)

    elif subcommand == "create":
        if args:
            handle_smart_create(client, channel_id, user_id, args, respond)
        else:
            show_create_modal(client, command.get("trigger_id"))

    elif subcommand == "list-domains" or subcommand == "domains":
        handle_list_domains(respond)

    elif subcommand == "list-roles" or subcommand == "roles":
        handle_list_roles(respond)

    else:
        # Try to parse as a smart prompt
        handle_smart_create(client, channel_id, user_id, text, respond)


def show_help(respond):
    """Show help message."""
    help_text = """
*Inventory Bot Commands*

`/inventory help` - Show this help message
`/inventory sync` - Run full sync (all hosts, all domains)
`/inventory create` - Open interactive dialog to create inventory
`/inventory create <prompt>` - Smart create (e.g., `mphpp for pubwxp`)
`/inventory list-domains` - List available domains
`/inventory list-roles` - List available roles

*Smart Prompt Examples:*
- `/inventory create mphpp for pubwxp` - mphpp servers in pubwxp domain
- `/inventory create all ts servers` - ts servers across all domains
- `/inventory create valxp` - all servers in valxp domain
- `/inventory create mim` - mim servers across all domains
"""
    respond(text=help_text)


def handle_full_sync(client, channel_id, user_id, respond):
    """Handle full sync request."""
    respond(text=f"<@{user_id}> Starting full sync...")

    # Post initial message to get thread_ts
    result = client.chat_postMessage(
        channel=channel_id,
        text="Starting full inventory sync..."
    )
    thread_ts = result.get("ts")

    # Run sync in background thread
    thread = threading.Thread(
        target=run_sync_job,
        args=(client, channel_id, thread_ts),
        kwargs={"inventory_name": "central inventory"}
    )
    thread.start()


def handle_smart_create(client, channel_id, user_id, prompt, respond):
    """Handle smart create with natural language prompt."""
    try:
        # Refresh cache and parse prompt
        _, domains, roles = refresh_etcd_cache()
        domain_filter, role_filter, inventory_name = parse_natural_language_prompt(
            prompt, domains, roles
        )

        # Build response
        filter_info = []
        if domain_filter:
            filter_info.append(f"Domain: `{domain_filter}`")
        if role_filter:
            filter_info.append(f"Role: `{role_filter}`")

        if not filter_info:
            filter_info.append("Full sync (no filters)")

        filter_str = " | ".join(filter_info)
        respond(text=f"<@{user_id}> Creating inventory `{inventory_name}`...\n{filter_str}")

        # Post initial message to get thread_ts
        result = client.chat_postMessage(
            channel=channel_id,
            text=f"Starting sync: `{inventory_name}`\n{filter_str}"
        )
        thread_ts = result.get("ts")

        # Run sync in background thread
        thread = threading.Thread(
            target=run_sync_job,
            args=(client, channel_id, thread_ts),
            kwargs={
                "domain_filter": domain_filter,
                "role_filter": role_filter,
                "inventory_name": inventory_name,
            }
        )
        thread.start()

    except Exception as e:
        respond(text=f"Error: {str(e)}")


def handle_list_domains(respond):
    """List available domains."""
    try:
        hosts, domains, _ = refresh_etcd_cache()

        # Count hosts per domain
        domain_counts = {}
        for host_info in hosts.values():
            domain = host_info.get("customer")
            if domain:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1

        # Sort by count
        sorted_domains = sorted(
            domain_counts.items(),
            key=lambda x: -x[1]
        )[:30]

        lines = ["*Available Domains (top 30 by host count):*"]
        for domain, count in sorted_domains:
            lines.append(f"  `{domain}` - {count} hosts")

        if len(domains) > 30:
            lines.append(f"\n_...and {len(domains) - 30} more domains_")

        respond(text="\n".join(lines))

    except Exception as e:
        respond(text=f"Error listing domains: {str(e)}")


def handle_list_roles(respond):
    """List available roles."""
    try:
        hosts, _, roles = refresh_etcd_cache()

        # Count hosts per role
        role_counts = {}
        for host_info in hosts.values():
            role = host_info.get("role")
            if role:
                role_counts[role] = role_counts.get(role, 0) + 1

        # Sort by count
        sorted_roles = sorted(
            role_counts.items(),
            key=lambda x: -x[1]
        )

        lines = ["*Available Roles:*"]
        for role, count in sorted_roles:
            lines.append(f"  `{role}` - {count} hosts")

        respond(text="\n".join(lines))

    except Exception as e:
        respond(text=f"Error listing roles: {str(e)}")


def show_create_modal(client, trigger_id):
    """Show the interactive create inventory modal."""
    try:
        # Refresh cache
        _, domains, roles = refresh_etcd_cache()

        # Build domain options (limit to 100 for Slack)
        sorted_domains = sorted(domains)[:100]
        domain_options = [
            {"text": {"type": "plain_text", "text": "All domains"}, "value": "all"}
        ]
        domain_options.extend([
            {"text": {"type": "plain_text", "text": d}, "value": d}
            for d in sorted_domains
        ])

        # Build role options
        sorted_roles = sorted(roles)[:100]
        role_options = [
            {"text": {"type": "plain_text", "text": "All roles"}, "value": "all"}
        ]
        role_options.extend([
            {"text": {"type": "plain_text", "text": r}, "value": r}
            for r in sorted_roles
        ])

        modal = {
            "type": "modal",
            "callback_id": "create_inventory_modal",
            "title": {"type": "plain_text", "text": "Create Inventory"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Create a new AWX inventory from etcd hosts."
                    }
                },
                {
                    "type": "input",
                    "block_id": "domain_block",
                    "element": {
                        "type": "static_select",
                        "action_id": "domain_select",
                        "placeholder": {"type": "plain_text", "text": "Select domain"},
                        "options": domain_options,
                        "initial_option": domain_options[0]
                    },
                    "label": {"type": "plain_text", "text": "Domain"}
                },
                {
                    "type": "input",
                    "block_id": "role_block",
                    "element": {
                        "type": "static_select",
                        "action_id": "role_select",
                        "placeholder": {"type": "plain_text", "text": "Select role"},
                        "options": role_options,
                        "initial_option": role_options[0]
                    },
                    "label": {"type": "plain_text", "text": "Role"}
                },
                {
                    "type": "input",
                    "block_id": "name_block",
                    "optional": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "name_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Leave empty for auto-generated name"
                        }
                    },
                    "label": {"type": "plain_text", "text": "Inventory Name (optional)"}
                }
            ]
        }

        client.views_open(trigger_id=trigger_id, view=modal)

    except SlackApiError as e:
        print(f"Error opening modal: {e.response['error']}")


@app.view("create_inventory_modal")
def handle_modal_submission(ack, body, client, view):
    """Handle create inventory modal submission."""
    ack()

    user_id = body["user"]["id"]

    # Extract values from modal
    values = view["state"]["values"]
    domain = values["domain_block"]["domain_select"]["selected_option"]["value"]
    role = values["role_block"]["role_select"]["selected_option"]["value"]
    custom_name = values["name_block"]["name_input"].get("value", "").strip()

    # Convert "all" to None
    domain_filter = None if domain == "all" else domain
    role_filter = None if role == "all" else role

    # Build inventory name
    if custom_name:
        inventory_name = custom_name
    elif role_filter and domain_filter:
        inventory_name = f"{role_filter}-{domain_filter}"
    elif role_filter:
        inventory_name = f"{role_filter}-all-domains"
    elif domain_filter:
        inventory_name = f"{domain_filter}-inventory"
    else:
        inventory_name = "central inventory"

    # Notify user via DM
    try:
        dm = client.conversations_open(users=[user_id])
        dm_channel = dm["channel"]["id"]

        # Build filter info
        filter_parts = []
        if domain_filter:
            filter_parts.append(f"Domain: `{domain_filter}`")
        if role_filter:
            filter_parts.append(f"Role: `{role_filter}`")
        filter_info = " | ".join(filter_parts) if filter_parts else "Full sync"

        result = client.chat_postMessage(
            channel=dm_channel,
            text=f"Starting inventory creation: `{inventory_name}`\n{filter_info}"
        )
        thread_ts = result.get("ts")

        # Use configured channel or DM
        channel_id = SLACK_CHANNEL_ID or dm_channel

        # Run sync in background thread
        thread = threading.Thread(
            target=run_sync_job,
            args=(client, channel_id, thread_ts),
            kwargs={
                "domain_filter": domain_filter,
                "role_filter": role_filter,
                "inventory_name": inventory_name,
            }
        )
        thread.start()

    except SlackApiError as e:
        print(f"Error sending DM: {e.response['error']}")


@app.event("app_mention")
def handle_mention(event, say):
    """Handle @mentions of the bot."""
    text = event.get("text", "").lower()

    if "help" in text:
        say("""
*Inventory Bot Commands*

Use `/inventory` followed by:
- `help` - Show help
- `sync` - Full sync
- `create` - Interactive dialog
- `create <prompt>` - Smart create
- `list-domains` - List domains
- `list-roles` - List roles
        """)
    else:
        say("Use `/inventory help` to see available commands.")


@app.event("message")
def handle_message(event, say):
    """Handle direct messages to the bot."""
    # Only respond to DMs (no channel_type means it's a DM)
    if event.get("channel_type") == "im":
        text = event.get("text", "").lower().strip()

        if text in ["help", "hi", "hello"]:
            say("Use `/inventory help` to see available commands.")


def main():
    """Start the Slack bot."""
    print("=" * 60)
    print("etcd-awx-sync Slack Bot")
    print("=" * 60)

    # Check AWX credentials
    check_required_env_vars()

    # Pre-warm the etcd cache
    print("\nPre-warming etcd cache...")
    try:
        hosts, domains, roles = refresh_etcd_cache(force=True)
        print(f"Cached {len(hosts)} hosts, {len(domains)} domains, {len(roles)} roles")
    except Exception as e:
        print(f"Warning: Could not pre-warm cache: {e}")

    print("\nStarting Slack bot in Socket Mode...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
