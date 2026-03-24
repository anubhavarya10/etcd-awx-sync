#!/usr/bin/env python3
"""
Slack alerts module for etcd-awx-sync.
Provides standalone functions for sending notifications to Slack.
Can be imported by other modules or used directly.
"""

import os
from typing import Optional, Dict, Any, List
from datetime import datetime

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:
    raise ImportError("slack_sdk is required. Install with: pip install slack_sdk")


# Configuration from environment variables
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")


def get_slack_client() -> WebClient:
    """Create and return a Slack WebClient."""
    if not SLACK_BOT_TOKEN:
        raise ValueError("SLACK_BOT_TOKEN environment variable is required")
    return WebClient(token=SLACK_BOT_TOKEN)


def send_sync_complete_alert(
    inventory_name: str,
    host_count: int,
    group_count: int,
    duration_seconds: float,
    domain_filter: Optional[str] = None,
    role_filter: Optional[str] = None,
    channel_id: Optional[str] = None,
    inventory_id: Optional[int] = None,
    awx_server: Optional[str] = None,
) -> bool:
    """
    Send a sync completion alert to Slack.

    Args:
        inventory_name: Name of the inventory that was synced
        host_count: Number of hosts synced
        group_count: Number of groups created/updated
        duration_seconds: Time taken for the sync in seconds
        domain_filter: Domain filter used (if any)
        role_filter: Role filter used (if any)
        channel_id: Slack channel ID (uses env var if not provided)
        inventory_id: AWX inventory ID for link generation
        awx_server: AWX server hostname for link generation

    Returns:
        True if message was sent successfully, False otherwise
    """
    channel = channel_id or SLACK_CHANNEL_ID
    if not channel:
        print("Warning: No Slack channel configured. Skipping alert.")
        return False

    try:
        client = get_slack_client()

        # Format duration
        if duration_seconds < 60:
            duration_str = f"{duration_seconds:.1f}s"
        else:
            minutes = int(duration_seconds // 60)
            seconds = int(duration_seconds % 60)
            duration_str = f"{minutes}m {seconds}s"

        # Build filter info
        filter_parts = []
        if domain_filter:
            filter_parts.append(f"Domain: `{domain_filter}`")
        if role_filter:
            filter_parts.append(f"Role: `{role_filter}`")
        filter_info = " | ".join(filter_parts) if filter_parts else "Full sync (no filters)"

        # Build inventory link if AWX info available
        inventory_link = inventory_name
        if inventory_id and awx_server:
            inventory_link = f"<https://{awx_server}/#/inventories/inventory/{inventory_id}/hosts|{inventory_name}>"

        # Build the message blocks
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Inventory Sync Complete",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Inventory:*\n{inventory_link}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Duration:*\n{duration_str}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Hosts:*\n{host_count}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Groups:*\n{group_count}"
                    }
                ]
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Filters:* {filter_info}"
                    }
                ]
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    }
                ]
            }
        ]

        response = client.chat_postMessage(
            channel=channel,
            text=f"Sync Complete: {inventory_name} - {host_count} hosts, {group_count} groups ({duration_str})",
            blocks=blocks
        )

        return response["ok"]

    except SlackApiError as e:
        print(f"Slack API error: {e.response['error']}")
        return False
    except Exception as e:
        print(f"Error sending Slack alert: {e}")
        return False


def send_error_alert(
    error_message: str,
    inventory_name: Optional[str] = None,
    context: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> bool:
    """
    Send an error alert to Slack.

    Args:
        error_message: The error message to display
        inventory_name: Name of the inventory being synced (if applicable)
        context: Additional context about what was happening
        channel_id: Slack channel ID (uses env var if not provided)

    Returns:
        True if message was sent successfully, False otherwise
    """
    channel = channel_id or SLACK_CHANNEL_ID
    if not channel:
        print("Warning: No Slack channel configured. Skipping alert.")
        return False

    try:
        client = get_slack_client()

        # Build context section
        context_parts = []
        if inventory_name:
            context_parts.append(f"*Inventory:* {inventory_name}")
        if context:
            context_parts.append(f"*Context:* {context}")

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Sync Error",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```{error_message}```"
                }
            }
        ]

        if context_parts:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": " | ".join(context_parts)
                    }
                ]
            })

        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Error occurred at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                }
            ]
        })

        response = client.chat_postMessage(
            channel=channel,
            text=f"Sync Error: {error_message[:100]}...",
            blocks=blocks
        )

        return response["ok"]

    except SlackApiError as e:
        print(f"Slack API error: {e.response['error']}")
        return False
    except Exception as e:
        print(f"Error sending Slack alert: {e}")
        return False


def send_progress_update(
    message: str,
    channel_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> Optional[str]:
    """
    Send a progress update message to Slack.

    Args:
        message: Progress message to send
        channel_id: Slack channel ID (uses env var if not provided)
        thread_ts: Thread timestamp to reply in thread

    Returns:
        Message timestamp (ts) if successful, None otherwise
    """
    channel = channel_id or SLACK_CHANNEL_ID
    if not channel:
        return None

    try:
        client = get_slack_client()

        kwargs = {
            "channel": channel,
            "text": message,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        response = client.chat_postMessage(**kwargs)
        return response.get("ts")

    except SlackApiError as e:
        print(f"Slack API error: {e.response['error']}")
        return None
    except Exception as e:
        print(f"Error sending Slack message: {e}")
        return None


def update_message(
    channel_id: str,
    ts: str,
    text: str,
    blocks: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """
    Update an existing Slack message.

    Args:
        channel_id: Slack channel ID
        ts: Message timestamp to update
        text: New message text
        blocks: Optional Block Kit blocks

    Returns:
        True if successful, False otherwise
    """
    try:
        client = get_slack_client()

        kwargs = {
            "channel": channel_id,
            "ts": ts,
            "text": text,
        }
        if blocks:
            kwargs["blocks"] = blocks

        response = client.chat_update(**kwargs)
        return response["ok"]

    except SlackApiError as e:
        print(f"Slack API error: {e.response['error']}")
        return False
    except Exception as e:
        print(f"Error updating Slack message: {e}")
        return False


# Allow running standalone for testing
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python slack_alerts.py [test-complete|test-error]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "test-complete":
        success = send_sync_complete_alert(
            inventory_name="test-inventory",
            host_count=100,
            group_count=5,
            duration_seconds=45.2,
            domain_filter="testdomain",
            role_filter="testrole",
        )
        print(f"Alert sent: {success}")

    elif command == "test-error":
        success = send_error_alert(
            error_message="Test error message",
            inventory_name="test-inventory",
            context="Testing error alerts",
        )
        print(f"Alert sent: {success}")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
