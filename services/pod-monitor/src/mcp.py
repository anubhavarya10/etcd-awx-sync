"""Pod Monitor MCP - Core logic for Kubernetes pod monitoring."""

import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass
from enum import Enum

from .k8s_client import K8sClient

logger = logging.getLogger(__name__)

# Slack message character limit
SLACK_MAX_LENGTH = 3800


class MCPResultStatus(str, Enum):
    """MCP result status."""
    SUCCESS = "success"
    ERROR = "error"
    PENDING = "pending"


@dataclass
class MCPResult:
    """Result of an MCP action."""
    status: MCPResultStatus
    message: str
    data: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "data": self.data or {},
        }


class PodMonitorMCP:
    """
    Pod Monitor MCP for Kubernetes pod health monitoring.

    This MCP:
    - Lists pods with status summaries
    - Shows detailed pod information (events, resources, images)
    - Retrieves pod logs
    - Identifies unhealthy pods
    - Provides namespace-level overviews
    """

    def __init__(self):
        self.k8s = K8sClient()

    @property
    def name(self) -> str:
        return "pod-monitor"

    @property
    def description(self) -> str:
        return "Monitor Kubernetes pod health, status, logs, and resources"

    def get_actions(self) -> List[Dict[str, Any]]:
        """Return list of available actions."""
        return [
            {
                "name": "list-pods",
                "description": "List all pods with status summary",
                "parameters": [
                    {"name": "namespace", "type": "string", "required": False,
                     "description": "Kubernetes namespace (default: 'default')"},
                ],
                "examples": ["list pods", "list pods in kube-system"],
            },
            {
                "name": "pod-details",
                "description": "Get detailed pod info including events, resources, and containers",
                "parameters": [
                    {"name": "pod", "type": "string", "required": True,
                     "description": "Pod name (supports partial/fuzzy matching)"},
                    {"name": "namespace", "type": "string", "required": False,
                     "description": "Kubernetes namespace (default: 'default')"},
                ],
                "examples": ["details slack-mcp-agent", "pod details service-manager"],
            },
            {
                "name": "pod-logs",
                "description": "Get recent container logs from a pod",
                "parameters": [
                    {"name": "pod", "type": "string", "required": True,
                     "description": "Pod name (supports partial/fuzzy matching)"},
                    {"name": "lines", "type": "integer", "required": False,
                     "description": "Number of log lines (default: 100)"},
                    {"name": "container", "type": "string", "required": False,
                     "description": "Specific container name (for multi-container pods)"},
                    {"name": "namespace", "type": "string", "required": False,
                     "description": "Kubernetes namespace (default: 'default')"},
                ],
                "examples": ["logs slack-mcp-agent", "logs service-manager 50"],
            },
            {
                "name": "unhealthy-pods",
                "description": "List only failing or unhealthy pods (CrashLoopBackOff, OOMKilled, high restarts, stuck Pending, etc.)",
                "parameters": [
                    {"name": "namespace", "type": "string", "required": False,
                     "description": "Kubernetes namespace (default: 'default')"},
                ],
                "examples": ["unhealthy pods", "show failing pods"],
            },
            {
                "name": "namespace-summary",
                "description": "Overview of a namespace: pod counts, health status, resource totals",
                "parameters": [
                    {"name": "namespace", "type": "string", "required": False,
                     "description": "Kubernetes namespace (default: 'default')"},
                ],
                "examples": ["namespace summary", "overview of kube-system"],
            },
        ]

    async def execute(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str = "",
        channel_id: str = "",
    ) -> MCPResult:
        """Execute an action."""
        logger.info(f"PodMonitor executing {action} with params: {parameters}")

        try:
            if action == "list-pods":
                return await self._handle_list_pods(parameters)
            elif action == "pod-details":
                return await self._handle_pod_details(parameters)
            elif action == "pod-logs":
                return await self._handle_pod_logs(parameters)
            elif action == "unhealthy-pods":
                return await self._handle_unhealthy_pods(parameters)
            elif action == "namespace-summary":
                return await self._handle_namespace_summary(parameters)
            else:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=f"Unknown action: {action}",
                )
        except Exception as e:
            logger.exception(f"Error executing {action}: {e}")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Error: {str(e)}",
            )

    async def _handle_list_pods(self, parameters: Dict[str, Any]) -> MCPResult:
        """List all pods with status summary."""
        namespace = parameters.get("namespace", "default")

        pods = await self.k8s.list_pods(namespace)
        if not pods:
            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=f"No pods found in namespace `{namespace}`.",
            )

        lines = [f"*Pods in `{namespace}` ({len(pods)}):*\n"]

        for pod in pods:
            phase = pod["phase"]
            restarts = pod["restarts"]
            ready = pod["ready"]
            age = pod["age"]
            name = pod["name"]

            # Status icon
            if phase == "Running" and ready.split("/")[0] == ready.split("/")[1]:
                icon = ":white_check_mark:"
            elif phase == "Succeeded":
                icon = ":heavy_check_mark:"
            elif phase == "Pending":
                icon = ":hourglass_flowing_sand:"
            elif phase == "Failed":
                icon = ":x:"
            else:
                icon = ":warning:"

            restart_str = f" | restarts: {restarts}" if restarts > 0 else ""
            lines.append(f"{icon} `{name}` {phase} ({ready}){restart_str} | {age}")

        message = "\n".join(lines)
        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=self._truncate(message),
            data={"namespace": namespace, "count": len(pods)},
        )

    async def _handle_pod_details(self, parameters: Dict[str, Any]) -> MCPResult:
        """Get detailed pod information."""
        pod_name = parameters.get("pod", "").strip()
        namespace = parameters.get("namespace", "default")

        if not pod_name:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Pod name is required.\n\nExample: `/pods details slack-mcp-agent`",
            )

        details = await self.k8s.get_pod_details(pod_name, namespace)
        if not details:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"No pod found matching `{pod_name}` in namespace `{namespace}`.",
            )

        lines = [f"*Pod Details: `{details['name']}`*\n"]

        # Basic info
        lines.append(f"*Status:* {details['phase']} | *Ready:* {details['ready']} | *Restarts:* {details['restarts']}")
        lines.append(f"*Node:* {details['node']} | *IP:* {details.get('ip', 'none')} | *Age:* {details['age']}")
        lines.append(f"*Service Account:* {details.get('service_account', 'default')}")

        # Containers & images
        if details.get("images"):
            lines.append("\n*Images:*")
            for img in details["images"]:
                lines.append(f"  `{img['container']}`: {img['image']}")

        # Resources
        if details.get("resources"):
            lines.append("\n*Resources:*")
            for res in details["resources"]:
                req = res.get("requests", {})
                lim = res.get("limits", {})
                if req or lim:
                    req_str = f"cpu={req.get('cpu', '-')}, mem={req.get('memory', '-')}" if req else "-"
                    lim_str = f"cpu={lim.get('cpu', '-')}, mem={lim.get('memory', '-')}" if lim else "-"
                    lines.append(f"  `{res['container']}`: requests({req_str}) limits({lim_str})")

        # Conditions
        if details.get("conditions"):
            lines.append("\n*Conditions:*")
            for cond in details["conditions"]:
                status_icon = ":white_check_mark:" if cond["status"] == "True" else ":x:"
                reason = f" ({cond['reason']})" if cond.get("reason") else ""
                lines.append(f"  {status_icon} {cond['type']}{reason}")

        # Events
        if details.get("events"):
            lines.append("\n*Recent Events:*")
            for event in details["events"][-5:]:
                type_icon = ":warning:" if event["type"] == "Warning" else ":information_source:"
                lines.append(f"  {type_icon} {event['reason']}: {event['message'][:100]}")

        # Other matches
        if details.get("other_matches"):
            lines.append(f"\n_Other matching pods: {', '.join(details['other_matches'])}_")

        message = "\n".join(lines)
        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=self._truncate(message),
            data={"pod": details["name"], "namespace": namespace},
        )

    async def _handle_pod_logs(self, parameters: Dict[str, Any]) -> MCPResult:
        """Get pod logs."""
        pod_name = parameters.get("pod", "").strip()
        namespace = parameters.get("namespace", "default")
        lines_count = int(parameters.get("lines", 100))
        container = parameters.get("container", "").strip() or None

        if not pod_name:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Pod name is required.\n\nExample: `/pods logs slack-mcp-agent`",
            )

        result = await self.k8s.get_pod_logs(
            pod_name, namespace, lines_count, container,
        )

        if result.get("error"):
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Error getting logs: {result['error']}",
            )

        container_str = f" (container: {container})" if container else ""
        lines = [
            f"*Logs for `{result['pod']}`{container_str}*",
            f"_(Last {lines_count} lines)_\n",
            f"```{result['logs'][:3000]}```",
        ]

        if len(result["logs"]) > 3000:
            lines.append("_... (output truncated)_")

        message = "\n".join(lines)
        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=message,
            data={"pod": result["pod"], "namespace": namespace, "lines": lines_count},
        )

    async def _handle_unhealthy_pods(self, parameters: Dict[str, Any]) -> MCPResult:
        """List unhealthy pods."""
        namespace = parameters.get("namespace", "default")

        unhealthy = await self.k8s.list_unhealthy_pods(namespace)

        if not unhealthy:
            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=f":white_check_mark: All pods in `{namespace}` are healthy!",
                data={"namespace": namespace, "unhealthy_count": 0},
            )

        lines = [f"*:warning: Unhealthy Pods in `{namespace}` ({len(unhealthy)}):*\n"]

        for pod in unhealthy:
            lines.append(f":x: `{pod['name']}` ({pod['phase']}, {pod['ready']} ready, {pod['restarts']} restarts)")
            for issue in pod.get("issues", []):
                lines.append(f"    - {issue}")

        message = "\n".join(lines)
        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=self._truncate(message),
            data={"namespace": namespace, "unhealthy_count": len(unhealthy)},
        )

    async def _handle_namespace_summary(self, parameters: Dict[str, Any]) -> MCPResult:
        """Get namespace overview."""
        namespace = parameters.get("namespace", "default")

        summary = await self.k8s.get_namespace_summary(namespace)

        health_icon = ":white_check_mark:" if summary["unhealthy"] == 0 else ":warning:"

        lines = [
            f"*Namespace Summary: `{summary['namespace']}`*\n",
            f"*Total Pods:* {summary['total_pods']} | *Containers:* {summary['total_containers']}",
            f"*Running:* {summary['running']} | *Pending:* {summary['pending']} | *Failed:* {summary['failed']} | *Succeeded:* {summary['succeeded']}",
            f"*Total Restarts:* {summary['total_restarts']}",
            f"{health_icon} *Unhealthy:* {summary['unhealthy']}",
        ]

        # Metrics if available
        metrics = summary.get("metrics")
        if metrics:
            lines.append(f"\n*Resource Usage:*")
            lines.append(f"  CPU: {metrics['total_cpu']} | Memory: {metrics['total_memory']}")
        else:
            lines.append("\n_Resource metrics unavailable (metrics-server not installed)_")

        message = "\n".join(lines)
        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=message,
            data=summary,
        )

    async def health_check(self) -> bool:
        """Check if MCP is healthy by verifying k8s API access."""
        try:
            # Try listing pods in default namespace as a connectivity check
            await self.k8s.list_pods("default")
            return True
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
            return False

    @staticmethod
    def _truncate(text: str, max_length: int = SLACK_MAX_LENGTH) -> str:
        """Truncate text to stay within Slack's message limit."""
        if len(text) <= max_length:
            return text
        return text[:max_length - 30] + "\n\n_... (output truncated)_"
