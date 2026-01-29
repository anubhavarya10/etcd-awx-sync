"""MCP implementation for etcd-awx-sync operations."""

import os
import asyncio
import time
import logging
from typing import Any, Dict, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor

from ..base import BaseMCP, MCPAction, MCPResult, MCPResultStatus

logger = logging.getLogger(__name__)

# Thread pool for running sync operations
_executor = ThreadPoolExecutor(max_workers=4)


class EtcdAwxMCP(BaseMCP):
    """
    MCP for etcd to AWX inventory synchronization.

    This MCP provides actions to:
    - Sync hosts from etcd to AWX
    - Create filtered inventories
    - List available domains and roles
    """

    def __init__(
        self,
        etcd_server: Optional[str] = None,
        etcd_port: Optional[int] = None,
        awx_server: Optional[str] = None,
    ):
        self.etcd_server = etcd_server or os.environ.get("ETCD_SERVER", "localhost")
        self.etcd_port = etcd_port or int(os.environ.get("ETCD_PORT", 2379))
        self.awx_server = awx_server or os.environ.get("AWX_SERVER", "localhost")

        # Cache for etcd data
        self._cache: Dict[str, Any] = {
            "hosts": {},
            "domains": set(),
            "roles": set(),
            "last_refresh": 0,
        }
        self._cache_ttl = 300  # 5 minutes

        super().__init__()

    @property
    def name(self) -> str:
        return "etcd-awx-sync"

    @property
    def description(self) -> str:
        return "Synchronize hosts from etcd service discovery to AWX inventory"

    def _setup_actions(self) -> None:
        """Register available actions."""

        self.register_action(MCPAction(
            name="sync",
            description="Run a full sync of all hosts from etcd to AWX inventory",
            parameters=[
                {
                    "name": "inventory_name",
                    "type": "string",
                    "description": "Custom name for the inventory (default: 'central inventory')",
                    "required": False,
                }
            ],
            requires_confirmation=True,
            examples=[
                "sync all inventory",
                "run full sync",
                "sync etcd to awx",
                "sync everything",
            ],
        ))

        self.register_action(MCPAction(
            name="create",
            description="Create a filtered inventory based on domain and/or role",
            parameters=[
                {
                    "name": "domain",
                    "type": "string",
                    "description": "Filter by domain/customer (e.g., 'pubwxp', 'valxp')",
                    "required": False,
                },
                {
                    "name": "role",
                    "type": "string",
                    "description": "Filter by role (e.g., 'mphpp', 'mim', 'ts')",
                    "required": False,
                },
                {
                    "name": "inventory_name",
                    "type": "string",
                    "description": "Custom name for the inventory",
                    "required": False,
                },
            ],
            requires_confirmation=True,
            examples=[
                "create inventory for mphpp in pubwxp",
                "mphpp servers for pubwxp",
                "all ts servers",
                "create valxp inventory",
                "mim for lolxp domain",
            ],
        ))

        self.register_action(MCPAction(
            name="list-domains",
            description="List all available domains/customers from etcd",
            parameters=[
                {
                    "name": "limit",
                    "type": "integer",
                    "description": "Maximum number of domains to show (default: 30)",
                    "required": False,
                }
            ],
            requires_confirmation=False,
            examples=[
                "list domains",
                "show available domains",
                "what domains are available",
                "list customers",
            ],
        ))

        self.register_action(MCPAction(
            name="list-roles",
            description="List all available roles from etcd",
            parameters=[
                {
                    "name": "limit",
                    "type": "integer",
                    "description": "Maximum number of roles to show (default: all)",
                    "required": False,
                }
            ],
            requires_confirmation=False,
            examples=[
                "list roles",
                "show available roles",
                "what roles are available",
                "list server roles",
            ],
        ))

        self.register_action(MCPAction(
            name="status",
            description="Show current status and statistics from etcd",
            parameters=[],
            requires_confirmation=False,
            examples=[
                "status",
                "show stats",
                "how many hosts",
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
        logger.info(f"Executing {action} with params: {parameters}")

        if action == "sync":
            return await self._handle_sync(parameters, user_id, channel_id)
        elif action == "create":
            return await self._handle_create(parameters, user_id, channel_id)
        elif action == "list-domains":
            return await self._handle_list_domains(parameters)
        elif action == "list-roles":
            return await self._handle_list_roles(parameters)
        elif action == "status":
            return await self._handle_status()
        else:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Unknown action: {action}"
            )

    async def _handle_sync(
        self,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Handle full sync action."""
        # Refresh cache to get host count
        await self._refresh_cache()
        host_count = len(self._cache["hosts"])

        inventory_name = parameters.get("inventory_name", "central inventory")

        return self.create_confirmation(
            action="sync",
            parameters={"inventory_name": inventory_name},
            user_id=user_id,
            channel_id=channel_id,
            confirmation_message=(
                f"*Confirm Full Sync*\n\n"
                f"This will sync *{host_count}* hosts from etcd to AWX.\n"
                f"Inventory: `{inventory_name}`\n\n"
                f"Do you want to proceed?"
            ),
        )

    async def _handle_create(
        self,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Handle create filtered inventory action."""
        domain = parameters.get("domain")
        role = parameters.get("role")
        inventory_name = parameters.get("inventory_name")

        if not domain and not role:
            # If no filters, treat as full sync
            return await self._handle_sync(parameters, user_id, channel_id)

        # Refresh cache
        await self._refresh_cache()

        # Validate domain
        if domain and domain.lower() not in {d.lower() for d in self._cache["domains"]}:
            available = sorted(self._cache["domains"])[:10]
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"Unknown domain: `{domain}`\n\n"
                    f"Available domains include: {', '.join(f'`{d}`' for d in available)}...\n"
                    f"Use `list domains` to see all available domains."
                )
            )

        # Validate role
        if role and role.lower() not in {r.lower() for r in self._cache["roles"]}:
            available = sorted(self._cache["roles"])[:10]
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"Unknown role: `{role}`\n\n"
                    f"Available roles include: {', '.join(f'`{r}`' for r in available)}...\n"
                    f"Use `list roles` to see all available roles."
                )
            )

        # Count matching hosts
        matching_count = self._count_matching_hosts(domain, role)

        if matching_count == 0:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"No hosts match the filters: domain=`{domain or 'all'}`, role=`{role or 'all'}`"
            )

        # Build inventory name
        if not inventory_name:
            if role and domain:
                inventory_name = f"{role}-{domain}"
            elif role:
                inventory_name = f"{role}-all-domains"
            elif domain:
                inventory_name = f"{domain}-inventory"

        # Build confirmation message
        filter_parts = []
        if domain:
            filter_parts.append(f"Domain: `{domain}`")
        if role:
            filter_parts.append(f"Role: `{role}`")

        return self.create_confirmation(
            action="create",
            parameters={
                "domain": domain,
                "role": role,
                "inventory_name": inventory_name,
            },
            user_id=user_id,
            channel_id=channel_id,
            confirmation_message=(
                f"*Confirm Inventory Creation*\n\n"
                f"Filters: {' | '.join(filter_parts)}\n"
                f"Matching hosts: *{matching_count}*\n"
                f"Inventory name: `{inventory_name}`\n\n"
                f"Do you want to proceed?"
            ),
        )

    async def _handle_list_domains(self, parameters: Dict[str, Any]) -> MCPResult:
        """Handle list domains action."""
        await self._refresh_cache()

        limit = parameters.get("limit", 30)
        hosts = self._cache["hosts"]
        domains = self._cache["domains"]

        # Count hosts per domain
        domain_counts = {}
        for host_info in hosts.values():
            d = host_info.get("customer")
            if d:
                domain_counts[d] = domain_counts.get(d, 0) + 1

        # Sort by count
        sorted_domains = sorted(
            domain_counts.items(),
            key=lambda x: -x[1]
        )[:limit]

        lines = ["*Available Domains* (by host count)\n"]
        for domain, count in sorted_domains:
            lines.append(f"  `{domain}` - {count} hosts")

        if len(domains) > limit:
            lines.append(f"\n_...and {len(domains) - limit} more domains_")

        lines.append(f"\n*Total:* {len(domains)} domains, {len(hosts)} hosts")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(lines),
        )

    async def _handle_list_roles(self, parameters: Dict[str, Any]) -> MCPResult:
        """Handle list roles action."""
        await self._refresh_cache()

        limit = parameters.get("limit")
        hosts = self._cache["hosts"]
        roles = self._cache["roles"]

        # Count hosts per role
        role_counts = {}
        for host_info in hosts.values():
            r = host_info.get("role")
            if r:
                role_counts[r] = role_counts.get(r, 0) + 1

        # Sort by count
        sorted_roles = sorted(
            role_counts.items(),
            key=lambda x: -x[1]
        )

        if limit:
            sorted_roles = sorted_roles[:limit]

        lines = ["*Available Roles* (by host count)\n"]
        for role, count in sorted_roles:
            lines.append(f"  `{role}` - {count} hosts")

        lines.append(f"\n*Total:* {len(roles)} roles")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(lines),
        )

    async def _handle_status(self) -> MCPResult:
        """Handle status action."""
        await self._refresh_cache()

        hosts = self._cache["hosts"]
        domains = self._cache["domains"]
        roles = self._cache["roles"]

        # Count hosts per role (top 5)
        role_counts = {}
        for host_info in hosts.values():
            r = host_info.get("role")
            if r:
                role_counts[r] = role_counts.get(r, 0) + 1

        top_roles = sorted(role_counts.items(), key=lambda x: -x[1])[:5]
        top_roles_str = ", ".join(f"`{r}` ({c})" for r, c in top_roles)

        message = (
            f"*etcd-awx-sync Status*\n\n"
            f"*etcd Server:* `{self.etcd_server}:{self.etcd_port}`\n"
            f"*AWX Server:* `{self.awx_server}`\n\n"
            f"*Statistics:*\n"
            f"  Total Hosts: *{len(hosts)}*\n"
            f"  Domains: *{len(domains)}*\n"
            f"  Roles: *{len(roles)}*\n\n"
            f"*Top Roles:* {top_roles_str}"
        )

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=message,
        )

    async def _execute_confirmed(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Execute action after user confirmation."""
        if action == "sync":
            return await self._run_sync(
                inventory_name=parameters.get("inventory_name", "central inventory"),
            )
        elif action == "create":
            return await self._run_sync(
                domain_filter=parameters.get("domain"),
                role_filter=parameters.get("role"),
                inventory_name=parameters.get("inventory_name"),
            )
        else:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Unknown confirmed action: {action}"
            )

    async def _run_sync(
        self,
        domain_filter: Optional[str] = None,
        role_filter: Optional[str] = None,
        inventory_name: Optional[str] = None,
    ) -> MCPResult:
        """Run the actual sync operation."""
        start_time = time.time()

        try:
            # Run in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                _executor,
                self._sync_worker,
                domain_filter,
                role_filter,
                inventory_name,
            )

            duration = time.time() - start_time

            if result["success"]:
                # Format duration
                if duration < 60:
                    duration_str = f"{duration:.1f}s"
                else:
                    minutes = int(duration // 60)
                    seconds = int(duration % 60)
                    duration_str = f"{minutes}m {seconds}s"

                # Build AWX link
                inv_link = result["inventory_name"]
                if result.get("inventory_id"):
                    inv_link = f"<http://{self.awx_server}/#/inventories/inventory/{result['inventory_id']}/hosts|{result['inventory_name']}>"

                message = (
                    f"*Sync Complete*\n\n"
                    f"*Inventory:* {inv_link}\n"
                    f"*Hosts:* {result['host_count']}\n"
                    f"*Groups:* {result['group_count']}\n"
                    f"*Duration:* {duration_str}"
                )

                if domain_filter or role_filter:
                    filter_parts = []
                    if domain_filter:
                        filter_parts.append(f"Domain: `{domain_filter}`")
                    if role_filter:
                        filter_parts.append(f"Role: `{role_filter}`")
                    message += f"\n*Filters:* {' | '.join(filter_parts)}"

                return MCPResult(
                    status=MCPResultStatus.SUCCESS,
                    message=message,
                    data=result,
                )
            else:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=f"Sync failed: {result.get('error', 'Unknown error')}",
                )

        except Exception as e:
            logger.exception("Error running sync")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Sync failed: {str(e)}",
            )

    def _sync_worker(
        self,
        domain_filter: Optional[str],
        role_filter: Optional[str],
        inventory_name: Optional[str],
    ) -> Dict[str, Any]:
        """Worker function to run sync in thread pool."""
        try:
            # Import here to avoid circular imports
            import sys
            etcd_awx_path = os.environ.get("ETCD_AWX_SYNC_PATH", "/app/etcd-awx-sync")
            if etcd_awx_path not in sys.path:
                sys.path.insert(0, etcd_awx_path)

            from etcd_to_awx import run_sync

            result = run_sync(
                domain_filter=domain_filter,
                role_filter=role_filter,
                inventory_name=inventory_name,
            )

            return {
                "success": True,
                "inventory_name": result["inventory_name"],
                "inventory_id": result["inventory_id"],
                "host_count": result["host_count"],
                "group_count": result["group_count"],
                "duration_seconds": result["duration_seconds"],
            }

        except Exception as e:
            logger.exception("Sync worker error")
            return {
                "success": False,
                "error": str(e),
            }

    async def _refresh_cache(self, force: bool = False) -> None:
        """Refresh the etcd cache if stale."""
        current_time = time.time()

        if not force and (current_time - self._cache["last_refresh"]) < self._cache_ttl:
            return

        try:
            loop = asyncio.get_event_loop()
            hosts, domains, roles = await loop.run_in_executor(
                _executor,
                self._fetch_from_etcd,
            )

            self._cache["hosts"] = hosts
            self._cache["domains"] = domains
            self._cache["roles"] = roles
            self._cache["last_refresh"] = current_time

            logger.info(f"Refreshed etcd cache: {len(hosts)} hosts, {len(domains)} domains, {len(roles)} roles")

        except Exception as e:
            logger.error(f"Failed to refresh etcd cache: {e}")

    def _fetch_from_etcd(self) -> Tuple[Dict, Set[str], Set[str]]:
        """Fetch data from etcd (runs in thread pool)."""
        try:
            import sys
            etcd_awx_path = os.environ.get("ETCD_AWX_SYNC_PATH", "/app/etcd-awx-sync")
            if etcd_awx_path not in sys.path:
                sys.path.insert(0, etcd_awx_path)

            from etcd_to_awx import get_hosts_from_etcd
            return get_hosts_from_etcd()

        except Exception as e:
            logger.error(f"Error fetching from etcd: {e}")
            return {}, set(), set()

    def _count_matching_hosts(
        self,
        domain_filter: Optional[str],
        role_filter: Optional[str],
    ) -> int:
        """Count hosts matching the given filters."""
        count = 0
        for host_info in self._cache["hosts"].values():
            # Check domain
            if domain_filter:
                host_domain = host_info.get("customer", "").lower()
                if domain_filter.lower() != host_domain:
                    continue

            # Check role
            if role_filter:
                host_role = host_info.get("role", "").lower()
                if role_filter.lower() != host_role:
                    continue

            count += 1

        return count

    async def health_check(self) -> bool:
        """Check if etcd is reachable."""
        try:
            await self._refresh_cache(force=True)
            return len(self._cache["hosts"]) > 0
        except Exception:
            return False
