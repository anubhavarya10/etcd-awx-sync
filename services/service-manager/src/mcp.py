"""Service Manager MCP - Core logic for service management."""

import os
import asyncio
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum

from .ssh_client import AsyncSSHClient
from .service_map import get_service_name, list_supported_roles

logger = logging.getLogger(__name__)


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


class ServiceManagerMCP:
    """
    Service Manager MCP for direct SSH-based service management.

    This MCP:
    - Connects to hosts via SSH (not through AWX)
    - Checks service status by role and domain
    - Starts, stops, restarts services
    - Fetches journalctl logs
    """

    def __init__(self):
        self.ssh_client = AsyncSSHClient()

        # etcd service URL for host lookups
        self.etcd_service_url = os.environ.get(
            "ETCD_SERVICE_URL",
            "http://slack-mcp-agent:8080"
        )

        # Cache for hosts (fetched from etcd service)
        self._hosts_cache: Dict[str, Any] = {}
        self._cache_ttl = 60  # 1 minute
        self._cache_time = 0

    @property
    def name(self) -> str:
        return "service-manager"

    @property
    def description(self) -> str:
        return "Check and manage services on servers via direct SSH"

    def _format_uptime(self, since_str: str) -> str:
        """
        Convert systemd timestamp to human-readable uptime.

        Input: "Wed 2026-03-02 10:15:23 UTC" or similar
        Output: "2d 5h 30m" or "45m" or "unknown"
        """
        if not since_str:
            return "unknown"

        try:
            from datetime import datetime, timezone
            import re

            # Parse systemd timestamp format: "Wed 2026-03-02 10:15:23 UTC"
            # or "2026-03-02 10:15:23 UTC"
            # Remove day name if present
            clean_str = re.sub(r'^[A-Za-z]+\s+', '', since_str.strip())

            # Try parsing with timezone
            for fmt in ["%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"]:
                try:
                    start_time = datetime.strptime(clean_str.replace(" UTC", ""), fmt.replace(" %Z", ""))
                    start_time = start_time.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                return since_str[:20]  # Fallback: return truncated original

            # Calculate uptime
            now = datetime.now(timezone.utc)
            delta = now - start_time

            days = delta.days
            hours, remainder = divmod(delta.seconds, 3600)
            minutes, _ = divmod(remainder, 60)

            # Format output
            parts = []
            if days > 0:
                parts.append(f"{days}d")
            if hours > 0:
                parts.append(f"{hours}h")
            if minutes > 0 and days == 0:  # Only show minutes if less than a day
                parts.append(f"{minutes}m")

            return " ".join(parts) if parts else "<1m"

        except Exception:
            return since_str[:20] if since_str else "unknown"

    @staticmethod
    def _parse_service_status(hostname: str, service: str, ssh_result) -> Dict[str, Any]:
        """Parse systemctl show output from an SSHResult into a status dict."""
        status_info = {
            "host": hostname,
            "service": service,
            "status": "unknown",
            "active_state": "",
            "sub_state": "",
            "since": "",
            "logs": "",
        }

        if not ssh_result.stdout and not ssh_result.success:
            status_info["status"] = "error"
            status_info["error"] = ssh_result.stderr or "Command failed"
            return status_info

        for line in ssh_result.stdout.split("\n"):
            if "=" in line:
                key, value = line.split("=", 1)
                if key == "ActiveState":
                    status_info["active_state"] = value
                    status_info["status"] = value
                elif key == "SubState":
                    status_info["sub_state"] = value
                elif key == "ActiveEnterTimestamp":
                    status_info["since"] = value

        return status_info

    def get_actions(self) -> List[Dict[str, Any]]:
        """Return list of available actions for LLM context."""
        return [
            {
                "name": "check-service",
                "description": "Check if a service is running on hosts by role and domain",
                "parameters": [
                    {"name": "role", "type": "string", "required": True, "description": "Server role (e.g., mim, mphpp, ts, ngx)"},
                    {"name": "domain", "type": "string", "required": True, "description": "Domain/customer name (e.g., hyxd, lionamxp)"},
                ],
                "examples": ["check mim in hyxd", "is mongooseim running on lionamxp"],
            },
            {
                "name": "start-service",
                "description": "Start a service on hosts",
                "parameters": [
                    {"name": "role", "type": "string", "required": True},
                    {"name": "domain", "type": "string", "required": True},
                    {"name": "host", "type": "string", "required": False, "description": "Specific host (optional)"},
                ],
                "examples": ["start mim on hyxd", "start nginx on ngx-pubwxp"],
            },
            {
                "name": "stop-service",
                "description": "Stop a service on hosts",
                "parameters": [
                    {"name": "role", "type": "string", "required": True},
                    {"name": "domain", "type": "string", "required": True},
                    {"name": "host", "type": "string", "required": False},
                ],
                "examples": ["stop mim on hyxd"],
            },
            {
                "name": "restart-service",
                "description": "Restart a service on hosts",
                "parameters": [
                    {"name": "role", "type": "string", "required": True},
                    {"name": "domain", "type": "string", "required": True},
                    {"name": "host", "type": "string", "required": False},
                ],
                "examples": ["restart mim on hyxd", "restart nginx on ngx-pubwxp"],
            },
            {
                "name": "service-logs",
                "description": "Get journalctl logs for a service",
                "parameters": [
                    {"name": "role", "type": "string", "required": True},
                    {"name": "domain", "type": "string", "required": True},
                    {"name": "host", "type": "string", "required": False},
                    {"name": "lines", "type": "integer", "required": False, "description": "Number of lines (default: 50)"},
                ],
                "examples": ["show logs for mim on hyxd", "get nginx logs from ngx-pubwxp"],
            },
            {
                "name": "list-service-roles",
                "description": "List all supported service roles",
                "parameters": [],
                "examples": ["list service roles", "what services can I manage"],
            },
            {
                "name": "get-version",
                "description": "Get software version for a service on hosts",
                "parameters": [
                    {"name": "role", "type": "string", "required": True, "description": "Server role (e.g., mim, mphpp, tps)"},
                    {"name": "domain", "type": "string", "required": True, "description": "Domain/customer name"},
                    {"name": "host", "type": "string", "required": False, "description": "Specific host (optional)"},
                ],
                "examples": ["version mim in hyxd", "what version of morpheus on pubgxp"],
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
        logger.info(f"ServiceManager executing {action} with params: {parameters}")

        try:
            if action == "check-service":
                return await self._handle_check_service(parameters)
            elif action == "start-service":
                return await self._handle_service_action(parameters, "start")
            elif action == "stop-service":
                return await self._handle_service_action(parameters, "stop")
            elif action == "restart-service":
                return await self._handle_service_action(parameters, "restart")
            elif action == "service-logs":
                return await self._handle_service_logs(parameters)
            elif action == "get-version":
                return await self._handle_get_version(parameters)
            elif action == "list-service-roles":
                return await self._handle_list_roles()
            else:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=f"Unknown action: {action}"
                )
        except Exception as e:
            logger.exception(f"Error executing {action}: {e}")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Error: {str(e)}"
            )

    async def _get_hosts_from_etcd(
        self,
        role: str,
        domain: str,
        specific_host: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch hosts matching role and domain from etcd.

        This queries the etcd directly (same as the etcd-awx-sync MCP does).
        """
        import etcd3

        etcd_server = os.environ.get("ETCD_SERVER", "10.0.25.44")
        etcd_port = int(os.environ.get("ETCD_PORT", 2379))
        etcd_prefix = os.environ.get("ETCD_PREFIX", "/discovery/")

        try:
            client = etcd3.client(host=etcd_server, port=etcd_port)
            hosts = {}

            for value, metadata in client.get_prefix(etcd_prefix):
                key = metadata.key.decode("utf-8")
                parts = key.split("/")

                if len(parts) < 5:
                    continue

                host_domain = parts[2]
                host_role = parts[3]
                hostname = parts[4]
                key_type = parts[5] if len(parts) > 5 else None

                # Filter by role and domain
                if host_role.lower() != role.lower():
                    continue
                if host_domain.lower() != domain.lower():
                    continue

                # Filter by specific host if provided
                # Supports: "mim3" -> matches "-3", "3" -> matches "-3", or direct substring
                if specific_host:
                    host_lower = hostname.lower()
                    filter_lower = specific_host.lower()

                    # Try different matching strategies
                    matched = False

                    # 1. Direct substring match
                    if filter_lower in host_lower:
                        matched = True
                    # 2. Extract number from filter (e.g., "mim3" -> "3") and match "-3" in hostname
                    elif any(c.isdigit() for c in filter_lower):
                        import re
                        nums = re.findall(r'\d+', filter_lower)
                        if nums:
                            # Match the last number in filter against hostname ending
                            num = nums[-1]
                            if f"-{num}." in host_lower or host_lower.endswith(f"-{num}"):
                                matched = True

                    if not matched:
                        continue

                if hostname not in hosts:
                    hosts[hostname] = {
                        "hostname": hostname,
                        "domain": host_domain,
                        "role": host_role,
                        "ip": None,
                    }

                # Extract IP
                if key_type == "viv_privip":
                    hosts[hostname]["ip"] = value.decode("utf-8").strip()
                elif key_type == "viv_pubip" and not hosts[hostname]["ip"]:
                    hosts[hostname]["ip"] = value.decode("utf-8").strip()

            # Use hostname as IP if no IP found
            for h in hosts.values():
                if not h["ip"]:
                    h["ip"] = h["hostname"]

            return sorted(hosts.values(), key=lambda h: h["hostname"])

        except Exception as e:
            logger.error(f"Error fetching hosts from etcd: {e}")
            return []

    async def _handle_check_service(self, parameters: Dict[str, Any]) -> MCPResult:
        """Check service status on hosts."""
        role = parameters.get("role", "").strip()
        domain = parameters.get("domain", "").strip()
        specific_host = parameters.get("host", "").strip()

        if not role or not domain:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Both role and domain are required.\n\nExample: `check mim in hyxd`"
            )

        # Build query string for display
        host_part = f" host {specific_host}" if specific_host else ""
        query_str = f"/svc check {role} in {domain}{host_part}"

        # Get hosts (optionally filtered by specific host)
        hosts = await self._get_hosts_from_etcd(role, domain, specific_host if specific_host else None)
        if not hosts:
            msg = f"*Query:* `{query_str}`\n\nNo hosts found for role `{role}` in domain `{domain}`"
            if specific_host:
                msg += f" matching `{specific_host}`"
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=msg + "."
            )

        # Get service name for this role
        service = get_service_name(role, domain)

        # Build status message header (query_str already defined above)
        lines = [
            f"*Query:* `{query_str}`",
            f"*Checking `{service}` on {len(hosts)} hosts in {domain}...*\n"
        ]

        # Check status on all hosts concurrently
        active_hosts = []
        failed_hosts = []
        inactive_hosts = []
        error_hosts = []

        inventory_name = f"{role}-{domain}"

        # Split hosts into Azure (batch via AWX) and regular (direct SSH)
        azure_hosts = [h for h in hosts if self.ssh_client._is_azure(h.get("ip", ""))]
        regular_hosts = [h for h in hosts if not self.ssh_client._is_azure(h.get("ip", ""))]

        results = []

        # Batch Azure hosts into a single AWX ad-hoc job
        if azure_hosts:
            status_cmd = f"systemctl show {service} --property=ActiveState,SubState,ActiveEnterTimestamp --no-pager"
            azure_hostnames = [h["hostname"] for h in azure_hosts]
            batch_results = await self.ssh_client.execute_azure_batch(
                azure_hostnames, status_cmd, inventory_name
            )
            for host_info in azure_hosts:
                hostname = host_info["hostname"]
                ssh_result = batch_results.get(hostname)
                if not ssh_result:
                    results.append({"host": hostname, "service": service, "status": "error", "error": "No result from AWX"})
                    continue
                status_info = self._parse_service_status(hostname, service, ssh_result)
                results.append(status_info)

        # Regular hosts use direct SSH concurrently
        if regular_hosts:
            async def check_host(host_info):
                return await self.ssh_client.check_service_status(
                    host_info["hostname"],
                    service,
                    host_info["ip"],
                )

            regular_results = await asyncio.gather(*[check_host(h) for h in regular_hosts])
            results.extend(regular_results)

        for result in results:
            status = result.get("status", "unknown")

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
                uptime_str = self._format_uptime(since)
                lines.append(f"  {h['host']}: active ({sub}) - up {uptime_str}")

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
        hosts = await self._get_hosts_from_etcd(role, domain, specific_host)
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

        # Execute the action
        inventory_name = f"{role}-{domain}"
        lines = [f"*{action.title()}ing `{service}` on {len(hosts)} host(s)...*\n"]

        success_hosts = []
        failed_hosts = []

        # Split Azure vs regular hosts
        azure_hosts = [h for h in hosts if self.ssh_client._is_azure(h.get("ip", ""))]
        regular_hosts = [h for h in hosts if not self.ssh_client._is_azure(h.get("ip", ""))]

        all_results = []  # list of (host_info, SSHResult)

        # Batch Azure hosts
        if azure_hosts:
            command = f"systemctl {action} {service}"
            azure_hostnames = [h["hostname"] for h in azure_hosts]
            batch_results = await self.ssh_client.execute_azure_batch(
                azure_hostnames, command, inventory_name
            )
            for host_info in azure_hosts:
                result = batch_results.get(host_info["hostname"])
                if result:
                    all_results.append((host_info, result))

        # Regular hosts via direct SSH
        if regular_hosts:
            async def manage_host(host_info):
                return host_info, await self.ssh_client.manage_service(
                    host_info["hostname"],
                    service,
                    action,
                    host_info["ip"],
                )

            regular_results = await asyncio.gather(*[manage_host(h) for h in regular_hosts])
            all_results.extend(regular_results)

        for host_info, result in all_results:
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
        hosts = await self._get_hosts_from_etcd(role, domain, specific_host)
        if not hosts:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"No hosts found for role `{role}` in domain `{domain}`"
            )

        # Get service name
        service = get_service_name(role, domain)

        # Get logs from first host (or specific host)
        inventory_name = f"{role}-{domain}"
        host = hosts[0]
        result = await self.ssh_client.get_service_logs(
            host["hostname"],
            service,
            lines_count,
            host["ip"],
            inventory_name=inventory_name,
        )

        if not result.success:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Failed to get logs from {host['hostname']}: {result.stderr}"
            )

        # Format output with query
        host_part = f" host {specific_host}" if specific_host else ""
        query_str = f"/svc logs {role} in {domain}{host_part}"
        output_lines = [
            f"*Query:* `{query_str}`",
            f"*Logs for `{service}` on {host['hostname']}*",
            f"_(Last {lines_count} lines)_\n",
            f"```{result.stdout[:3000]}```",
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

    async def _handle_get_version(self, parameters: Dict[str, Any]) -> MCPResult:
        """Get software version from etcd for hosts - dynamically discovers all versions."""
        import etcd3
        import re

        role = parameters.get("role", "").strip()
        domain = parameters.get("domain", "").strip()
        specific_host = parameters.get("host", "").strip()
        software_filter = parameters.get("software", "").strip().lower()

        # Build query string for display
        host_part = f" host {specific_host}" if specific_host else ""
        query_str = f"/svc version {role} in {domain}{host_part}"

        if not role or not domain:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"*Query:* `{query_str}`\n\nBoth role and domain are required.\n\nExample: `/svc version mim in hyxd`"
            )

        service = get_service_name(role, domain)

        # Connect to etcd and dynamically discover ALL version keys
        etcd_server = os.environ.get("ETCD_SERVER", "10.0.25.44")
        etcd_port = int(os.environ.get("ETCD_PORT", 2379))
        etcd_prefix = os.environ.get("ETCD_PREFIX", "/discovery/")

        try:
            client = etcd3.client(host=etcd_server, port=etcd_port)

            # Structure: {hostname: {version_key: version_value}}
            host_versions = {}
            found_hosts = set()

            for value, metadata in client.get_prefix(etcd_prefix):
                key = metadata.key.decode("utf-8")
                parts = key.split("/")

                if len(parts) < 6:
                    continue

                host_domain = parts[2]
                host_role = parts[3]
                hostname = parts[4]
                key_type = parts[5]

                # Filter by role and domain
                if host_role.lower() != role.lower():
                    continue
                if host_domain.lower() != domain.lower():
                    continue

                # Filter by specific host if provided
                if specific_host:
                    host_lower = hostname.lower()
                    filter_lower = specific_host.lower()
                    matched = False
                    if filter_lower in host_lower:
                        matched = True
                    elif any(c.isdigit() for c in filter_lower):
                        nums = re.findall(r'\d+', filter_lower)
                        if nums:
                            num = nums[-1]
                            if f"-{num}." in host_lower or host_lower.endswith(f"-{num}"):
                                matched = True
                    if not matched:
                        continue

                found_hosts.add(hostname)

                # Check if this is a version key
                if key_type.startswith("version_"):
                    val = value.decode("utf-8").strip()
                    # Skip empty, None, or "not installed" values
                    if val and val != "None" and "not installed" not in val.lower() and "no packages found" not in val.lower():
                        version_name = key_type.replace("version_", "")

                        # Apply software filter if specified
                        if software_filter and software_filter not in version_name.lower():
                            continue

                        if hostname not in host_versions:
                            host_versions[hostname] = {}
                        host_versions[hostname][version_name] = val

        except Exception as e:
            logger.error(f"Error fetching versions from etcd: {e}")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"*Query:* `{query_str}`\n\nError fetching versions: {str(e)}"
            )

        if not host_versions:
            msg = f"*Query:* `{query_str}`\n\nNo version information found for `{role}` in `{domain}`."
            if found_hosts:
                msg += f"\n\n_Found {len(found_hosts)} hosts but no version data in etcd._"
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=msg
            )

        # Analyze versions - find the primary/most relevant version for this role
        # and check if all hosts have the same version
        all_version_keys = set()
        for host_data in host_versions.values():
            all_version_keys.update(host_data.keys())

        # Prioritize version keys based on role
        role_lower = role.lower()
        priority_map = {
            "mim": ["mongooseim"],
            "mphpp": ["morpheus"],
            "mphhos": ["morpheus"],
            "tps": ["transcription_proxy_service"],
            "srouter": ["ssr_router_core"],
            "sdecoder": ["ssr_decoder"],
            "scapture": ["ssr_capture_fastpath"],
            "sconductor": ["conductor"],
            "www": ["vivox_backend_api"],
            "www5": ["vivox_backend_api"],
            "harjo": ["harjo", "vivox_backend_bin"],
        }

        # Find the most relevant version key
        primary_keys = priority_map.get(role_lower, [])
        relevant_key = None
        for pk in primary_keys:
            if pk in all_version_keys:
                relevant_key = pk
                break

        # If no priority key found, use the first available (excluding generic ones)
        if not relevant_key:
            for k in sorted(all_version_keys):
                if k not in ["vivox_backend_bin", "vivox_backend_mproxy"]:
                    relevant_key = k
                    break
            if not relevant_key and all_version_keys:
                relevant_key = sorted(all_version_keys)[0]

        lines = [
            f"*Query:* `{query_str}`",
            f"*Software Versions for `{role}` in `{domain}`:*\n"
        ]

        if relevant_key:
            # Show the primary version
            versions_for_key = {}
            for hostname, host_data in host_versions.items():
                if relevant_key in host_data:
                    versions_for_key[hostname] = host_data[relevant_key]

            unique_versions = set(versions_for_key.values())

            if len(unique_versions) == 1:
                version = list(unique_versions)[0]
                lines.append(f":white_check_mark: *{relevant_key}:* `{version}` (all {len(versions_for_key)} hosts)")
            elif len(unique_versions) > 1:
                lines.append(f":warning: *{relevant_key}* - {len(unique_versions)} different versions:")
                for hostname, version in sorted(versions_for_key.items()):
                    # Shorten hostname for display
                    short_name = hostname.split('.')[0]
                    lines.append(f"  `{short_name}`: `{version}`")

            # Show other available version keys if any
            other_keys = [k for k in all_version_keys if k != relevant_key]
            if other_keys and len(other_keys) <= 5:
                lines.append(f"\n_Other versions available: {', '.join(sorted(other_keys))}_")
        else:
            lines.append("No version data found.")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(lines),
            data={
                "role": role,
                "domain": domain,
                "hosts": len(host_versions),
                "versions": host_versions,
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
