"""MCP implementation for direct SSH service management."""

import os
import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..base import BaseMCP, MCPAction, MCPResult, MCPResultStatus
from .ssh_client import AsyncSSHClient
from .service_map import get_service_name, get_all_services_for_role, list_supported_roles

logger = logging.getLogger(__name__)


class ServiceManagerMCP(BaseMCP):
    """
    MCP for direct SSH-based service management.

    This MCP provides actions to:
    - Check service status on hosts by role/domain
    - Start, stop, restart services
    - View service logs
    - Manage services without AWX
    """

    def __init__(self):
        # SSH client for direct connections
        self.ssh_client = AsyncSSHClient()

        # Reference to etcd cache (will be set after initialization)
        self._etcd_mcp = None

        super().__init__()

    @property
    def name(self) -> str:
        return "service-manager"

    @property
    def description(self) -> str:
        return "Check and manage services on servers via direct SSH"

    def set_etcd_mcp(self, etcd_mcp):
        """Set reference to EtcdAwxMCP for host lookups."""
        self._etcd_mcp = etcd_mcp

    def _setup_actions(self) -> None:
        """Register available actions."""

        self.register_action(MCPAction(
            name="check-service",
            description="Check if a service is running on hosts by role and domain",
            parameters=[
                {
                    "name": "role",
                    "type": "string",
                    "description": "Server role (e.g., mim, mphpp, ts, ngx)",
                    "required": True,
                },
                {
                    "name": "domain",
                    "type": "string",
                    "description": "Domain/customer name (e.g., hyxd, lionamxp, pubwxp)",
                    "required": True,
                },
            ],
            requires_confirmation=False,
            examples=[
                "check mim in hyxd",
                "check service mphpp on pubwxp",
                "is mongooseim running on lionamxp",
                "check nginx status on nwxp",
            ],
        ))

        self.register_action(MCPAction(
            name="start-service",
            description="Start a service on hosts",
            parameters=[
                {
                    "name": "role",
                    "type": "string",
                    "description": "Server role",
                    "required": True,
                },
                {
                    "name": "domain",
                    "type": "string",
                    "description": "Domain/customer name",
                    "required": True,
                },
                {
                    "name": "host",
                    "type": "string",
                    "description": "Specific host (optional, defaults to all hosts)",
                    "required": False,
                },
            ],
            requires_confirmation=True,
            examples=[
                "start mim on hyxd",
                "start mongooseim on mim-hyxd-010101-1",
                "start nginx service on ngx-pubwxp",
            ],
        ))

        self.register_action(MCPAction(
            name="stop-service",
            description="Stop a service on hosts",
            parameters=[
                {
                    "name": "role",
                    "type": "string",
                    "description": "Server role",
                    "required": True,
                },
                {
                    "name": "domain",
                    "type": "string",
                    "description": "Domain/customer name",
                    "required": True,
                },
                {
                    "name": "host",
                    "type": "string",
                    "description": "Specific host (optional)",
                    "required": False,
                },
            ],
            requires_confirmation=True,
            examples=[
                "stop mim on hyxd",
                "stop redis on redis-pubwxp-010101-1",
            ],
        ))

        self.register_action(MCPAction(
            name="restart-service",
            description="Restart a service on hosts",
            parameters=[
                {
                    "name": "role",
                    "type": "string",
                    "description": "Server role",
                    "required": True,
                },
                {
                    "name": "domain",
                    "type": "string",
                    "description": "Domain/customer name",
                    "required": True,
                },
                {
                    "name": "host",
                    "type": "string",
                    "description": "Specific host (optional)",
                    "required": False,
                },
            ],
            requires_confirmation=True,
            examples=[
                "restart mim on hyxd",
                "restart nginx on ngx-pubwxp",
                "restart morpheus on mphpp-bnxp-010103-1",
            ],
        ))

        self.register_action(MCPAction(
            name="service-logs",
            description="Get journalctl logs for a service",
            parameters=[
                {
                    "name": "role",
                    "type": "string",
                    "description": "Server role",
                    "required": True,
                },
                {
                    "name": "domain",
                    "type": "string",
                    "description": "Domain/customer name",
                    "required": True,
                },
                {
                    "name": "host",
                    "type": "string",
                    "description": "Specific host (optional, defaults to first host)",
                    "required": False,
                },
                {
                    "name": "lines",
                    "type": "integer",
                    "description": "Number of log lines (default: 50)",
                    "required": False,
                },
            ],
            requires_confirmation=False,
            examples=[
                "show logs for mim on hyxd",
                "get nginx logs from ngx-pubwxp",
                "service logs mphpp on bnxp last 100 lines",
            ],
        ))

        self.register_action(MCPAction(
            name="list-service-roles",
            description="List all supported service roles",
            parameters=[],
            requires_confirmation=False,
            examples=[
                "list service roles",
                "what services can I manage",
                "show supported roles",
            ],
        ))

    async def execute(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Execute an action."""
        logger.info(f"ServiceManager executing {action} with params: {parameters}")

        if action == "check-service":
            return await self._handle_check_service(parameters)
        elif action == "start-service":
            return await self._handle_service_action(parameters, "start", user_id, channel_id)
        elif action == "stop-service":
            return await self._handle_service_action(parameters, "stop", user_id, channel_id)
        elif action == "restart-service":
            return await self._handle_service_action(parameters, "restart", user_id, channel_id)
        elif action == "service-logs":
            return await self._handle_service_logs(parameters)
        elif action == "list-service-roles":
            return await self._handle_list_roles()
        else:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Unknown action: {action}"
            )

    async def _get_hosts_by_role_domain(
        self,
        role: str,
        domain: str,
        specific_host: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get hosts matching role and domain from etcd cache.

        Returns list of dicts with hostname, ip, etc.
        """
        if not self._etcd_mcp:
            logger.error("EtcdAwxMCP not configured")
            return []

        # Access etcd cache
        cache = getattr(self._etcd_mcp, "_cache", {})
        hosts_cache = cache.get("hosts", {})

        matching_hosts = []
        role_lower = role.lower()
        domain_lower = domain.lower()

        for hostname, info in hosts_cache.items():
            host_role = info.get("role", "").lower()
            host_domain = info.get("domain", info.get("customer", "")).lower()

            # Match role and domain
            if host_role != role_lower or host_domain != domain_lower:
                continue

            # If specific host requested, filter further
            if specific_host:
                if specific_host.lower() not in hostname.lower():
                    continue

            # Get IP address
            ip = info.get("ip") or info.get("private_ip") or info.get("public_ip") or hostname

            matching_hosts.append({
                "hostname": hostname,
                "ip": ip,
                "role": host_role,
                "domain": host_domain,
            })

        return sorted(matching_hosts, key=lambda h: h["hostname"])

    async def _handle_check_service(self, parameters: Dict[str, Any]) -> MCPResult:
        """Check service status on hosts."""
        role = parameters.get("role", "").strip()
        domain = parameters.get("domain", "").strip()

        if not role or not domain:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Both role and domain are required.\n\nExample: `check mim in hyxd`"
            )

        # Get hosts
        hosts = await self._get_hosts_by_role_domain(role, domain)
        if not hosts:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"No hosts found for role `{role}` in domain `{domain}`.\n\nUse `list domains {role}` to see available domains."
            )

        # Get service name for this role
        service = get_service_name(role, domain)

        # Build status message header
        lines = [f"*Checking `{service}` on {len(hosts)} hosts in {domain}...*\n"]

        # Check status on all hosts concurrently
        active_hosts = []
        failed_hosts = []
        inactive_hosts = []
        error_hosts = []

        async def check_host(host_info):
            return await self.ssh_client.check_service_status(
                host_info["hostname"],
                service,
                host_info["ip"],
            )

        results = await asyncio.gather(*[check_host(h) for h in hosts])

        for result in results:
            status = result.get("status", "unknown")
            host = result.get("host", "unknown")

            if status == "active":
                active_hosts.append(result)
            elif status == "failed":
                failed_hosts.append(result)
            elif status == "inactive":
                inactive_hosts.append(result)
            else:
                error_hosts.append(result)

        # Format output
        if active_hosts:
            lines.append(f"*:white_check_mark: Active ({len(active_hosts)}):*")
            for h in active_hosts:
                sub = h.get("sub_state", "")
                since = h.get("since", "")
                since_short = since.split(" ")[0] if since else ""
                lines.append(f"  {h['host']}: active ({sub}) since {since_short}")

        if inactive_hosts:
            lines.append(f"\n*:warning: Inactive ({len(inactive_hosts)}):*")
            for h in inactive_hosts:
                lines.append(f"  {h['host']}: inactive")

        if failed_hosts:
            lines.append(f"\n*:x: Failed ({len(failed_hosts)}):*")
            for h in failed_hosts:
                lines.append(f"  {h['host']}: failed")
                if h.get("logs"):
                    lines.append(f"  ```{h['logs'][:500]}```")

        if error_hosts:
            lines.append(f"\n*:exclamation: Error ({len(error_hosts)}):*")
            for h in error_hosts:
                error_msg = h.get("error", "Connection failed")
                lines.append(f"  {h['host']}: {error_msg}")

        # Summary
        lines.append(f"\n*Summary:* {len(active_hosts)} active, {len(inactive_hosts)} inactive, {len(failed_hosts)} failed, {len(error_hosts)} errors")

        # Suggest actions if there are issues
        if failed_hosts or inactive_hosts:
            lines.append(f"\n_To restart: `restart {role} on {domain}`_")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(lines),
            data={
                "role": role,
                "domain": domain,
                "service": service,
                "active": len(active_hosts),
                "inactive": len(inactive_hosts),
                "failed": len(failed_hosts),
                "errors": len(error_hosts),
            }
        )

    async def _handle_service_action(
        self,
        parameters: Dict[str, Any],
        action: str,
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Handle start/stop/restart service actions."""
        role = parameters.get("role", "").strip()
        domain = parameters.get("domain", "").strip()
        specific_host = parameters.get("host", "").strip()

        if not role or not domain:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Both role and domain are required.\n\nExample: `{action} mim on hyxd`"
            )

        # Get hosts
        hosts = await self._get_hosts_by_role_domain(role, domain, specific_host)
        if not hosts:
            msg = f"No hosts found for role `{role}` in domain `{domain}`"
            if specific_host:
                msg += f" matching `{specific_host}`"
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=msg
            )

        # Get service name
        service = get_service_name(role, domain)

        # Check if confirmation is needed
        action_def = self.get_action(f"{action}-service")
        if action_def and action_def.requires_confirmation:
            host_list = ", ".join([h["hostname"] for h in hosts[:5]])
            if len(hosts) > 5:
                host_list += f" (+{len(hosts) - 5} more)"

            return self.create_confirmation(
                action=f"{action}-service",
                parameters=parameters,
                user_id=user_id,
                channel_id=channel_id,
                confirmation_message=(
                    f"Are you sure you want to *{action}* `{service}` on {len(hosts)} host(s)?\n\n"
                    f"*Hosts:* {host_list}"
                ),
            )

        # Execute the action
        return await self._execute_service_action(hosts, service, action)

    async def _execute_confirmed(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Execute after user confirms."""
        if action in ("start-service", "stop-service", "restart-service"):
            action_verb = action.replace("-service", "")
            role = parameters.get("role", "")
            domain = parameters.get("domain", "")
            specific_host = parameters.get("host", "")

            hosts = await self._get_hosts_by_role_domain(role, domain, specific_host)
            service = get_service_name(role, domain)

            return await self._execute_service_action(hosts, service, action_verb)

        return MCPResult(
            status=MCPResultStatus.ERROR,
            message=f"Unknown confirmed action: {action}"
        )

    async def _execute_service_action(
        self,
        hosts: List[Dict[str, Any]],
        service: str,
        action: str,
    ) -> MCPResult:
        """Execute service action on hosts."""
        lines = [f"*{action.title()}ing `{service}` on {len(hosts)} host(s)...*\n"]

        success_hosts = []
        failed_hosts = []

        async def manage_host(host_info):
            return await self.ssh_client.manage_service(
                host_info["hostname"],
                service,
                action,
                host_info["ip"],
            )

        results = await asyncio.gather(*[manage_host(h) for h in hosts])

        for host_info, result in zip(hosts, results):
            if result.success:
                success_hosts.append(host_info["hostname"])
                lines.append(f":white_check_mark: {host_info['hostname']}: {action} successful")
            else:
                failed_hosts.append(host_info["hostname"])
                error = result.stderr or result.output or "Unknown error"
                lines.append(f":x: {host_info['hostname']}: failed - {error[:100]}")

        # Summary
        lines.append(f"\n*Result:* {len(success_hosts)} succeeded, {len(failed_hosts)} failed")

        status = MCPResultStatus.SUCCESS if not failed_hosts else MCPResultStatus.ERROR
        return MCPResult(
            status=status,
            message="\n".join(lines),
            data={
                "action": action,
                "service": service,
                "success": success_hosts,
                "failed": failed_hosts,
            }
        )

    async def _handle_service_logs(self, parameters: Dict[str, Any]) -> MCPResult:
        """Get service logs from a host."""
        role = parameters.get("role", "").strip()
        domain = parameters.get("domain", "").strip()
        specific_host = parameters.get("host", "").strip()
        lines_count = int(parameters.get("lines", 50))

        if not role or not domain:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Both role and domain are required.\n\nExample: `logs mim on hyxd`"
            )

        # Get hosts
        hosts = await self._get_hosts_by_role_domain(role, domain, specific_host)
        if not hosts:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"No hosts found for role `{role}` in domain `{domain}`"
            )

        # Get service name
        service = get_service_name(role, domain)

        # Get logs from first host (or specific host)
        host = hosts[0]
        result = await self.ssh_client.get_service_logs(
            host["hostname"],
            service,
            lines_count,
            host["ip"],
        )

        if not result.success:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Failed to get logs from {host['hostname']}: {result.stderr}"
            )

        # Format output
        output_lines = [
            f"*Logs for `{service}` on {host['hostname']}*",
            f"_(Last {lines_count} lines)_\n",
            f"```{result.stdout[:3000]}```",  # Limit output size
        ]

        if len(result.stdout) > 3000:
            output_lines.append("_... (output truncated)_")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(output_lines),
            data={
                "host": host["hostname"],
                "service": service,
                "lines": lines_count,
            }
        )

    async def _handle_list_roles(self) -> MCPResult:
        """List all supported service roles."""
        roles = list_supported_roles()

        lines = ["*Supported Service Roles:*\n"]
        for role in roles:
            service = get_service_name(role)
            lines.append(f"  `{role}` - {service}")

        lines.append(f"\n*Total:* {len(roles)} roles")
        lines.append("\n_Example: `check mim in hyxd` or `restart nginx on ngx-pubwxp`_")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(lines),
            data={"roles": roles}
        )

    async def health_check(self) -> bool:
        """Check if MCP is healthy."""
        # Check if SSH key exists (for non-Azure hosts)
        ssh_key_path = os.environ.get("SSH_KEY_PATH", "/app/ssh/id_rsa")
        key_exists = os.path.exists(ssh_key_path)

        # Check if Azure password is configured
        azure_password = os.environ.get("AZURE_SUDO_PASSWORD")

        # At least one auth method should be available
        return key_exists or bool(azure_password)
