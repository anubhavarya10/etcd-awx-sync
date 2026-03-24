"""Background pod health alerter - checks pods periodically and posts to Slack."""

import os
import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

from slack_sdk.web.async_client import AsyncWebClient

from .k8s_client import K8sClient

logger = logging.getLogger(__name__)


def _issue_hash(issue: str) -> str:
    """Return first 8 hex chars of the md5 of an issue string."""
    return hashlib.md5(issue.encode()).hexdigest()[:8]


@dataclass
class AlertState:
    """Tracks state for a single pod+issue alert."""
    thread_ts: Optional[str] = None       # ts of the first Slack message (for threading)
    channel_id: Optional[str] = None      # channel the alert was posted to
    resolved: bool = False
    resolved_by: Optional[str] = None
    paused_until: Optional[datetime] = None
    paused_by: Optional[str] = None
    occurrence_count: int = 0
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PodAlerter:
    """
    Background task that periodically checks pod health and sends
    alerts to Slack via the Slack Web API (Block Kit with buttons).

    Alert conditions:
    - CrashLoopBackOff
    - OOMKilled
    - High restarts (>5)
    - Stuck Pending (>5 min)
    - ImagePullBackOff / ErrImagePull
    - Not ready for >5 min

    First alert posts a Block Kit message with action buttons.
    Subsequent alerts are threaded under the first message.
    """

    def __init__(self, k8s_client: K8sClient):
        self.k8s = k8s_client

        # Slack Web API client
        self.bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        self.channel_id = os.environ.get("SLACK_ALERT_CHANNEL_ID", "")
        self.slack: Optional[AsyncWebClient] = None

        # Fallback: webhook (kept for backward compat if no bot token)
        self.webhook_url = os.environ.get("SLACK_ALERT_WEBHOOK_URL", "")

        self.check_interval = int(os.environ.get("ALERT_CHECK_INTERVAL", "120"))  # seconds
        self.namespace = os.environ.get("ALERT_NAMESPACE", "default")
        self.cooldown_seconds = int(os.environ.get("ALERT_COOLDOWN", "3600"))  # 1 hour
        self.enabled = os.environ.get("ALERTING_ENABLED", "true").lower() == "true"

        # Per pod+issue alert state
        self._alert_states: Dict[Tuple[str, str], AlertState] = {}
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the background alerting loop."""
        if not self.enabled:
            logger.info("Alerting is disabled (ALERTING_ENABLED=false)")
            return

        if self.bot_token and self.channel_id:
            self.slack = AsyncWebClient(token=self.bot_token)
            logger.info("Alerter using Slack Web API (Bot Token + Channel ID)")
        elif self.webhook_url:
            logger.warning(
                "SLACK_BOT_TOKEN or SLACK_ALERT_CHANNEL_ID not set, "
                "falling back to webhook (no threading/buttons)"
            )
        else:
            logger.warning("No Slack credentials configured, alerting disabled")
            return

        logger.info(
            f"Starting pod alerter: namespace={self.namespace}, "
            f"interval={self.check_interval}s, cooldown={self.cooldown_seconds}s"
        )
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        """Stop the background alerting loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------
    # Public methods for HTTP endpoints
    # ------------------------------------------------------------------

    def resolve_alert(self, pod: str, issue: str, user_id: str = "") -> bool:
        """Mark an alert as resolved. Returns True if alert was found."""
        key = (pod, issue)
        state = self._alert_states.get(key)
        if not state:
            # Try matching by issue hash
            state, key = self._find_by_hash(pod, issue)
        if state and not state.resolved:
            state.resolved = True
            state.resolved_by = user_id
            logger.info(f"Alert resolved: {key} by {user_id}")
            return True
        return False

    def pause_alert(self, pod: str, issue: str, hours: int, user_id: str = "") -> bool:
        """Pause an alert for the given number of hours. Returns True if found."""
        key = (pod, issue)
        state = self._alert_states.get(key)
        if not state:
            state, key = self._find_by_hash(pod, issue)
        if state:
            state.paused_until = datetime.now(timezone.utc) + timedelta(hours=hours)
            state.paused_by = user_id
            logger.info(f"Alert paused: {key} for {hours}h by {user_id}")
            return True
        return False

    def get_alert_status(self) -> list:
        """Return a list of all active (non-resolved) alert states."""
        now = datetime.now(timezone.utc)
        result = []
        for (pod, issue), state in self._alert_states.items():
            if state.resolved:
                continue
            paused = state.paused_until and now < state.paused_until
            result.append({
                "pod": pod,
                "issue": issue,
                "occurrence_count": state.occurrence_count,
                "first_seen": state.first_seen.isoformat(),
                "last_seen": state.last_seen.isoformat(),
                "paused": paused,
                "paused_until": state.paused_until.isoformat() if state.paused_until else None,
                "thread_ts": state.thread_ts,
            })
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_by_hash(self, pod: str, issue_or_hash: str):
        """Find alert state by pod name and issue hash."""
        for (p, iss), state in self._alert_states.items():
            if p == pod and _issue_hash(iss) == issue_or_hash:
                return state, (p, iss)
        return None, None

    async def _run_loop(self):
        """Main alerting loop."""
        await asyncio.sleep(30)

        while True:
            try:
                await self._check_and_alert()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in alerter loop: {e}")

            await asyncio.sleep(self.check_interval)

    async def _check_and_alert(self):
        """Check for unhealthy pods and send alerts."""
        unhealthy = await self.k8s.list_unhealthy_pods(self.namespace)
        now = datetime.now(timezone.utc)

        # Track which pod+issue combos are currently unhealthy
        current_unhealthy_keys = set()

        for pod in unhealthy:
            pod_name = pod["name"]
            for issue in pod.get("issues", []):
                key = (pod_name, issue)
                current_unhealthy_keys.add(key)
                state = self._alert_states.get(key)

                if state and state.resolved:
                    # Issue reappeared after resolve
                    if state.paused_until and now < state.paused_until:
                        # Previous pause is still active — keep it, just un-resolve
                        state.resolved = False
                        state.resolved_by = None
                        continue  # Still paused, don't alert
                    else:
                        # No active pause — start fresh
                        del self._alert_states[key]
                        state = None

                if state and state.paused_until:
                    if now < state.paused_until:
                        continue  # Still paused
                    else:
                        # Pause expired, resume alerting
                        state.paused_until = None
                        state.paused_by = None

                if state is None:
                    # First occurrence — post Block Kit message
                    state = AlertState(first_seen=now, last_seen=now)
                    self._alert_states[key] = state
                    await self._send_first_alert(pod, issue, state)
                else:
                    # Subsequent occurrence — check cooldown, then post thread update
                    elapsed = (now - state.last_seen).total_seconds()
                    if elapsed >= self.cooldown_seconds:
                        state.last_seen = now
                        await self._send_followup_alert(pod, issue, state)

        # Auto-resolve: pods that were unhealthy but are now healthy
        # Skip paused alerts — user explicitly said "don't bother me about this"
        for key, state in list(self._alert_states.items()):
            if key not in current_unhealthy_keys and not state.resolved:
                if state.paused_until and now < state.paused_until:
                    continue  # Don't auto-resolve paused alerts
                state.resolved = True
                state.resolved_by = "auto"
                await self._post_auto_resolved(key[0], key[1], state)

        # Clean up old resolved states (>24h)
        expired = [
            key for key, st in self._alert_states.items()
            if st.resolved and (now - st.last_seen).total_seconds() > 86400
        ]
        for key in expired:
            del self._alert_states[key]

    # ------------------------------------------------------------------
    # Slack messaging
    # ------------------------------------------------------------------

    async def _send_first_alert(self, pod: dict, issue: str, state: AlertState):
        """Post the first Block Kit alert with buttons."""
        pod_name = pod["name"]
        phase = pod["phase"]
        restarts = pod["restarts"]
        ready = pod["ready"]
        ihash = _issue_hash(issue)

        state.occurrence_count = 1

        if self.slack:
            blocks = self._build_alert_blocks(pod_name, issue, phase, ready, restarts, ihash)
            fallback_text = f":rotating_light: Pod Alert: {pod_name} — {issue}"

            try:
                resp = await self.slack.chat_postMessage(
                    channel=self.channel_id,
                    text=fallback_text,
                    blocks=blocks,
                )
                state.thread_ts = resp["ts"]
                state.channel_id = self.channel_id
                logger.info(f"First alert sent for {pod_name}: {issue} (ts={state.thread_ts})")
            except Exception as e:
                logger.error(f"Failed to send first alert for {pod_name}: {e}")
        else:
            await self._send_webhook_fallback(pod, issue)

    async def _send_followup_alert(self, pod: dict, issue: str, state: AlertState):
        """Post a thread reply for a recurring alert."""
        pod_name = pod["name"]
        phase = pod["phase"]
        restarts = pod["restarts"]
        ready = pod["ready"]
        state.occurrence_count += 1

        elapsed = datetime.now(timezone.utc) - state.first_seen
        elapsed_str = self._format_duration(elapsed)

        text = (
            f":repeat: Still unhealthy (occurrence #{state.occurrence_count}, "
            f"{elapsed_str} since first alert)\n"
            f"*Status:* {phase} | *Ready:* {ready} | *Restarts:* {restarts}"
        )

        if self.slack and state.thread_ts:
            try:
                await self.slack.chat_postMessage(
                    channel=state.channel_id or self.channel_id,
                    text=text,
                    thread_ts=state.thread_ts,
                )
                logger.info(f"Follow-up alert #{state.occurrence_count} for {pod_name}: {issue}")
            except Exception as e:
                logger.error(f"Failed to send follow-up for {pod_name}: {e}")
        else:
            await self._send_webhook_fallback(pod, issue)

    async def _post_auto_resolved(self, pod_name: str, issue: str, state: AlertState):
        """Post a thread message when a pod self-resolves (becomes healthy)."""
        if not self.slack or not state.thread_ts:
            return

        text = f":white_check_mark: *Auto-resolved* — `{pod_name}` is now healthy.\n_Issue `{issue}` cleared after {state.occurrence_count} occurrence(s)._"
        try:
            await self.slack.chat_postMessage(
                channel=state.channel_id or self.channel_id,
                text=text,
                thread_ts=state.thread_ts,
            )
            # Update original message to remove buttons and show resolved
            await self._update_message_resolved(
                state.channel_id or self.channel_id,
                state.thread_ts,
                pod_name,
                issue,
                "auto (pod healthy)",
            )
        except Exception as e:
            logger.error(f"Failed to post auto-resolved for {pod_name}: {e}")

    async def update_message_resolved(self, channel_id: str, ts: str, pod_name: str, issue: str, resolved_by: str):
        """Public wrapper for updating a message to show resolved status."""
        await self._update_message_resolved(channel_id, ts, pod_name, issue, resolved_by)

    async def _update_message_resolved(self, channel_id: str, ts: str, pod_name: str, issue: str, resolved_by: str):
        """Update the original alert message to remove buttons and show resolved."""
        if not self.slack:
            return
        try:
            resolved_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":white_check_mark: ~*Pod Alert: `{pod_name}`*~\n"
                            f"*Issue:* {issue}\n"
                            f"*Resolved by:* {resolved_by}"
                        ),
                    },
                },
            ]
            await self.slack.chat_update(
                channel=channel_id,
                ts=ts,
                text=f"Resolved: {pod_name} — {issue}",
                blocks=resolved_blocks,
            )
        except Exception as e:
            logger.error(f"Failed to update message as resolved: {e}")

    async def update_message_paused(self, channel_id: str, ts: str, pod_name: str, issue: str, paused_by: str, hours: int):
        """Update the original alert message to show paused status."""
        if not self.slack:
            return
        try:
            duration_str = f"{hours // 24} day(s)" if hours >= 24 else f"{hours}h"
            paused_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":pause_button: *Pod Alert: `{pod_name}`* (paused)\n"
                            f"*Issue:* {issue}\n"
                            f"*Paused for:* {duration_str} by {paused_by}"
                        ),
                    },
                },
            ]
            await self.slack.chat_update(
                channel=channel_id,
                ts=ts,
                text=f"Paused: {pod_name} — {issue}",
                blocks=paused_blocks,
            )
        except Exception as e:
            logger.error(f"Failed to update message as paused: {e}")

    # ------------------------------------------------------------------
    # Block Kit builder
    # ------------------------------------------------------------------

    def _build_alert_blocks(self, pod_name: str, issue: str, phase: str, ready: str, restarts: int, ihash: str) -> list:
        """Build Block Kit blocks for the first alert message."""
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":rotating_light: *Pod Alert: `{pod_name}`*\n"
                        f"*Issue:* {issue}\n"
                        f"*Status:* {phase} | *Ready:* {ready} | *Restarts:* {restarts}\n"
                        f"*Namespace:* {self.namespace}"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Resolve"},
                        "style": "primary",
                        "action_id": f"alert_resolve_{pod_name}_{ihash}",
                        "value": f"{pod_name}|{issue}",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Pause 1d"},
                        "action_id": f"alert_pause_1d_{pod_name}_{ihash}",
                        "value": f"{pod_name}|{issue}",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Pause 1w"},
                        "action_id": f"alert_pause_1w_{pod_name}_{ihash}",
                        "value": f"{pod_name}|{issue}",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Self-Resolve (AI)"},
                        "action_id": f"alert_selfheal_{pod_name}_{ihash}",
                        "value": f"{pod_name}|{issue}",
                    },
                ],
            },
        ]

    # ------------------------------------------------------------------
    # Webhook fallback (no threading/buttons)
    # ------------------------------------------------------------------

    async def _send_webhook_fallback(self, pod: dict, issue: str):
        """Send a plain alert via webhook (legacy fallback)."""
        import httpx

        pod_name = pod["name"]
        phase = pod["phase"]
        restarts = pod["restarts"]
        ready = pod["ready"]

        text = (
            f":rotating_light: *Pod Alert: `{pod_name}`*\n"
            f"*Issue:* {issue}\n"
            f"*Status:* {phase} | *Ready:* {ready} | *Restarts:* {restarts}\n"
            f"*Namespace:* {self.namespace}"
        )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    self.webhook_url,
                    json={"text": text},
                )
                if response.status_code != 200:
                    logger.warning(f"Slack webhook returned {response.status_code}: {response.text}")
                else:
                    logger.info(f"Webhook alert sent for {pod_name}: {issue}")
        except Exception as e:
            logger.error(f"Failed to send webhook alert for {pod_name}: {e}")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _format_duration(td: timedelta) -> str:
        """Format a timedelta as a human-readable string."""
        total_seconds = int(td.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        remaining_minutes = minutes % 60
        if hours < 24:
            return f"{hours}h{remaining_minutes}m" if remaining_minutes else f"{hours}h"
        days = hours // 24
        remaining_hours = hours % 24
        return f"{days}d{remaining_hours}h" if remaining_hours else f"{days}d"
