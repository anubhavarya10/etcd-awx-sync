"""MCP implementation for etcd-awx-sync operations."""

import os
import asyncio
import time
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
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
            description="List all available domains/customers from etcd, optionally filtered by role",
            parameters=[
                {
                    "name": "role",
                    "type": "string",
                    "description": "Filter domains by role (e.g., 'mphpp', 'mim')",
                    "required": False,
                },
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
                "list domains for mphpp",
                "which domains have mim servers",
            ],
        ))

        self.register_action(MCPAction(
            name="list-roles",
            description="List all available roles from etcd, optionally filtered by domain",
            parameters=[
                {
                    "name": "domain",
                    "type": "string",
                    "description": "Filter roles by domain (e.g., 'bnxp', 'pubwxp')",
                    "required": False,
                },
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
                "list roles in bnxp",
                "what roles does pubwxp have",
                "show roles for valxp",
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
            ],
        ))

        self.register_action(MCPAction(
            name="count",
            description="Count hosts by role and/or domain",
            parameters=[
                {"name": "role", "type": "string", "description": "Role to count", "required": False},
                {"name": "domain", "type": "string", "description": "Domain to count", "required": False},
            ],
            requires_confirmation=False,
            examples=[
                "how many mphpp does bnxp have",
                "how many hosts does lolxp have",
                "count mim in caxp",
            ],
        ))

        self.register_action(MCPAction(
            name="count-domains",
            description="Count how many domains have a specific role",
            parameters=[
                {"name": "role", "type": "string", "description": "Role to search for", "required": True},
            ],
            requires_confirmation=False,
            examples=[
                "how many domains have ngx",
                "how many domains have srouter",
            ],
        ))

        self.register_action(MCPAction(
            name="update",
            description="Update an existing inventory with fresh data from etcd",
            parameters=[
                {"name": "domain", "type": "string", "description": "Domain filter", "required": False},
                {"name": "role", "type": "string", "description": "Role filter", "required": False},
                {"name": "inventory_name", "type": "string", "description": "Name of inventory to update", "required": False},
            ],
            requires_confirmation=False,
            examples=[
                "update inventory mim-nwxp",
                "update mphpp-pubwxp inventory",
                "refresh mim inventory for nwxp",
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
        elif action == "count":
            return await self._handle_count(parameters)
        elif action == "count-domains":
            return await self._handle_count_domains(parameters)
        elif action == "update":
            # Update is like create but with force_update=True
            parameters["force_update"] = True
            return await self._handle_create(parameters, user_id, channel_id)
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

        logger.info(f"Running full sync: {host_count} hosts, inventory={inventory_name}")

        # Run sync directly (no confirmation)
        return await self._run_sync(
            inventory_name=inventory_name,
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
        force_update = parameters.get("force_update", False)

        logger.info(f"_handle_create called with: domain={domain}, role={role}, inventory_name={inventory_name}")

        # Refresh cache to validate inputs
        await self._refresh_cache()

        # Validate role if provided
        if role:
            role_lower = role.lower()
            valid_roles = {r.lower(): r for r in self._cache["roles"]}
            if role_lower not in valid_roles:
                suggestions = self._find_similar(role, self._cache["roles"], limit=5)
                suggestion_str = ", ".join(f"`{s}`" for s in suggestions) if suggestions else "none found"
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=(
                        f"⚠️ *Unknown role:* `{role}`\n\n"
                        f"*Did you mean:* {suggestion_str}\n\n"
                        f"Use `list roles` to see all available roles.\n\n"
                        f"_Ready for next task_"
                    )
                )
            # Use the correct case from cache
            role = valid_roles[role_lower]

        # Validate domain if provided
        if domain:
            domain_lower = domain.lower()
            valid_domains = {d.lower(): d for d in self._cache["domains"]}
            if domain_lower not in valid_domains:
                suggestions = self._find_similar(domain, self._cache["domains"], limit=5)
                suggestion_str = ", ".join(f"`{s}`" for s in suggestions) if suggestions else "none found"
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=(
                        f"⚠️ *Unknown domain:* `{domain}`\n\n"
                        f"*Did you mean:* {suggestion_str}\n\n"
                        f"Use `list domains` to see all available domains.\n\n"
                        f"_Ready for next task_"
                    )
                )
            # Use the correct case from cache
            domain = valid_domains[domain_lower]

        # Build inventory name if not provided
        if not inventory_name:
            if role and domain:
                inventory_name = f"{role}-{domain}"
            elif role:
                inventory_name = f"{role}-all-domains"
            elif domain:
                inventory_name = f"{domain}-inventory"
            else:
                inventory_name = "central inventory"

        # Count matching hosts
        matching_count = self._count_matching_hosts(domain, role)

        if matching_count == 0:
            hint = ""
            if domain:
                hint = f"Try `list roles in {domain}`"
            elif role:
                hint = f"Try `list domains for {role}`"
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"⚠️ *No hosts found*\n\n"
                    f"Filters: Role=`{role or 'all'}` | Domain=`{domain or 'all'}`\n\n"
                    f"Both role and domain are valid, but no hosts match this combination.\n"
                    f"{hint}\n\n"
                    f"_Ready for next task_"
                )
            )

        # Build filter description for status message
        filter_parts = []
        if role:
            filter_parts.append(f"Role: `{role}`")
        if domain:
            filter_parts.append(f"Domain: `{domain}`")
        filter_str = " | ".join(filter_parts) if filter_parts else "ALL hosts"

        # Check if inventory already exists in AWX (unless force_update)
        if not force_update:
            existing = await self._check_inventory_exists(inventory_name)
            if existing:
                inv_id = existing.get("id")
                host_count = existing.get("total_hosts", 0)
                inv_link = f"<http://{self.awx_server}/#/inventories/inventory/{inv_id}/hosts|{inventory_name}>"
                return MCPResult(
                    status=MCPResultStatus.SUCCESS,
                    message=(
                        f"ℹ️ *Inventory already exists*\n\n"
                        f"*Inventory:* {inv_link}\n"
                        f"*Current hosts:* {host_count}\n\n"
                        f"To update: `update inventory {inventory_name}`\n\n"
                        f"_Ready for next task_"
                    ),
                    data={"inventory_id": inv_id, "inventory_name": inventory_name, "exists": True}
                )

        logger.info(f"Running sync: {filter_str}, {matching_count} hosts, inventory={inventory_name}")

        # Run sync directly
        return await self._run_sync(
            domain_filter=domain,
            role_filter=role,
            inventory_name=inventory_name,
        )

    def _find_similar(self, term: str, candidates: set, limit: int = 5) -> List[str]:
        """Find similar terms using prefix matching and edit distance."""
        term_lower = term.lower()
        matches = []

        # First, try prefix matching
        prefix_matches = [c for c in candidates if c.lower().startswith(term_lower[:2])]
        matches.extend(sorted(prefix_matches)[:limit])

        # If not enough matches, try substring matching
        if len(matches) < limit:
            substring_matches = [c for c in candidates if term_lower in c.lower() or c.lower() in term_lower]
            for m in substring_matches:
                if m not in matches:
                    matches.append(m)
                if len(matches) >= limit:
                    break

        # If still not enough, add some based on similar length
        if len(matches) < limit:
            by_length = sorted(candidates, key=lambda x: abs(len(x) - len(term)))
            for m in by_length:
                if m not in matches:
                    matches.append(m)
                if len(matches) >= limit:
                    break

        return matches[:limit]

    async def _check_inventory_exists(self, inventory_name: str) -> Optional[Dict[str, Any]]:
        """Check if an inventory already exists in AWX."""
        try:
            import requests

            # Get AWX credentials from environment
            awx_server = os.environ.get("AWX_SERVER", "localhost")
            awx_username = os.environ.get("AWX_USERNAME")
            awx_password = os.environ.get("AWX_PASSWORD")

            if not awx_username or not awx_password:
                logger.warning("AWX credentials not set, skipping inventory existence check")
                return None

            url = f"http://{awx_server}/api/v2/inventories/"
            response = requests.get(
                url,
                params={"name": inventory_name},
                auth=(awx_username, awx_password),
                timeout=10
            )

            if response.status_code == 200:
                results = response.json().get("results", [])
                if results:
                    inv = results[0]
                    return {
                        "id": inv.get("id"),
                        "name": inv.get("name"),
                        "total_hosts": inv.get("total_hosts", 0),
                        "total_groups": inv.get("total_groups", 0),
                    }

            return None

        except Exception as e:
            logger.error(f"Error checking inventory existence: {e}")
            return None

    async def _handle_list_domains(self, parameters: Dict[str, Any]) -> MCPResult:
        """Handle list domains action."""
        await self._refresh_cache()

        limit = parameters.get("limit", 30)
        role_filter = parameters.get("role")
        hosts = self._cache["hosts"]
        domains = self._cache["domains"]

        # Count hosts per domain (optionally filtered by role)
        domain_counts = {}
        total_filtered = 0
        for host_info in hosts.values():
            d = host_info.get("customer")
            r = host_info.get("role")

            # Apply role filter if specified
            if role_filter and r and r.lower() != role_filter.lower():
                continue

            if d:
                domain_counts[d] = domain_counts.get(d, 0) + 1
                total_filtered += 1

        # Sort by count
        sorted_domains = sorted(
            domain_counts.items(),
            key=lambda x: -x[1]
        )[:limit]

        if role_filter:
            lines = [f"*Domains with `{role_filter}` servers* (by host count)\n"]
        else:
            lines = ["*Available Domains* (by host count)\n"]

        for domain, count in sorted_domains:
            lines.append(f"  `{domain}` - {count} hosts")

        filtered_domain_count = len(domain_counts)
        if filtered_domain_count > limit:
            lines.append(f"\n_...and {filtered_domain_count - limit} more domains_")

        if role_filter:
            lines.append(f"\n*Total:* {filtered_domain_count} domains with `{role_filter}`, {total_filtered} hosts")
        else:
            lines.append(f"\n*Total:* {len(domains)} domains, {len(hosts)} hosts")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(lines),
        )

    async def _handle_list_roles(self, parameters: Dict[str, Any]) -> MCPResult:
        """Handle list roles action."""
        await self._refresh_cache()

        limit = parameters.get("limit")
        domain_filter = parameters.get("domain")
        hosts = self._cache["hosts"]
        roles = self._cache["roles"]

        # Count hosts per role (optionally filtered by domain)
        role_counts = {}
        total_filtered = 0
        for host_info in hosts.values():
            r = host_info.get("role")
            d = host_info.get("customer")

            # Apply domain filter if specified
            if domain_filter and d and d.lower() != domain_filter.lower():
                continue

            if r:
                role_counts[r] = role_counts.get(r, 0) + 1
                total_filtered += 1

        # Sort by count
        sorted_roles = sorted(
            role_counts.items(),
            key=lambda x: -x[1]
        )

        if limit:
            sorted_roles = sorted_roles[:limit]

        if domain_filter:
            lines = [f"*Roles in `{domain_filter}` domain* (by host count)\n"]
        else:
            lines = ["*Available Roles* (by host count)\n"]

        for role, count in sorted_roles:
            lines.append(f"  `{role}` - {count} hosts")

        filtered_role_count = len(role_counts)
        if domain_filter:
            lines.append(f"\n*Total:* {filtered_role_count} roles in `{domain_filter}`, {total_filtered} hosts")
        else:
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

    async def _handle_count(self, parameters: Dict[str, Any]) -> MCPResult:
        """Handle count hosts action."""
        await self._refresh_cache()

        role_filter = parameters.get("role")
        domain_filter = parameters.get("domain")
        hosts = self._cache["hosts"]

        # Count matching hosts
        count = 0
        for host_info in hosts.values():
            r = host_info.get("role")
            d = host_info.get("customer")

            # Apply filters
            if role_filter and r and r.lower() != role_filter.lower():
                continue
            if domain_filter and d and d.lower() != domain_filter.lower():
                continue

            count += 1

        # Build response message
        if role_filter and domain_filter:
            message = f"*{count}* `{role_filter}` hosts in `{domain_filter}`"
        elif domain_filter:
            message = f"*{count}* total hosts in `{domain_filter}`"
        elif role_filter:
            message = f"*{count}* `{role_filter}` hosts across all domains"
        else:
            message = f"*{count}* total hosts"

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=message,
            data={"count": count, "role": role_filter, "domain": domain_filter},
        )

    async def _handle_count_domains(self, parameters: Dict[str, Any]) -> MCPResult:
        """Handle count domains with role action."""
        await self._refresh_cache()

        role_filter = parameters.get("role")
        if not role_filter:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Role is required for counting domains",
            )

        hosts = self._cache["hosts"]

        # Find domains that have this role
        domains_with_role = set()
        for host_info in hosts.values():
            r = host_info.get("role")
            d = host_info.get("customer")

            if r and r.lower() == role_filter.lower() and d:
                domains_with_role.add(d)

        count = len(domains_with_role)

        # List first few domains
        domain_list = sorted(domains_with_role)[:10]
        if len(domains_with_role) > 10:
            domain_preview = ", ".join(f"`{d}`" for d in domain_list) + f"... and {len(domains_with_role) - 10} more"
        else:
            domain_preview = ", ".join(f"`{d}`" for d in domain_list) if domain_list else "none"

        message = f"*{count}* domains have `{role_filter}` servers\n\nDomains: {domain_preview}"

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=message,
            data={"count": count, "role": role_filter, "domains": list(domains_with_role)},
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
        timeout_seconds: int = 300,  # 5 minute timeout
    ) -> MCPResult:
        """Run the actual sync operation with timeout."""
        start_time = time.time()

        try:
            # Run in thread pool to avoid blocking, with timeout
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    _executor,
                    self._sync_worker,
                    domain_filter,
                    role_filter,
                    inventory_name,
                ),
                timeout=timeout_seconds
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

                # Build filter description
                filter_parts = []
                if domain_filter:
                    filter_parts.append(f"Domain: `{domain_filter}`")
                if role_filter:
                    filter_parts.append(f"Role: `{role_filter}`")
                filter_str = " | ".join(filter_parts) if filter_parts else "All hosts"

                message = (
                    f"✅ *Task Complete*\n\n"
                    f"*Inventory:* {inv_link}\n"
                    f"*Hosts:* {result['host_count']}\n"
                    f"*Groups:* {result['group_count']}\n"
                    f"*Filters:* {filter_str}\n"
                    f"*Duration:* {duration_str}\n\n"
                    f"_Ready for next task_"
                )

                return MCPResult(
                    status=MCPResultStatus.SUCCESS,
                    message=message,
                    data=result,
                )
            else:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=f"❌ *Sync failed:* {result.get('error', 'Unknown error')}\n\n_Ready for next task_",
                )

        except asyncio.TimeoutError:
            duration = time.time() - start_time
            logger.error(f"Sync timed out after {duration:.1f}s")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"⏱️ *Task timed out* after {timeout_seconds//60} minutes.\n\nThe AWX server may be slow or unreachable.\n\n_Ready for next task_",
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
            import etcd3

            client = etcd3.client(
                host=self.etcd_server,
                port=self.etcd_port,
            )

            hosts = {}
            domains = set()
            roles = set()

            # Fetch all keys under /discovery/
            prefix = os.environ.get("ETCD_PREFIX", "/discovery/")

            for value, metadata in client.get_prefix(prefix):
                if metadata is None:
                    continue

                key = metadata.key.decode('utf-8') if isinstance(metadata.key, bytes) else metadata.key

                # Key structure: /discovery/<customer>/<role>/<hostname>/<key_type>
                # Example: /discovery/bnxp/mphpp/mphpp-bnxp-010103-1-3f6e3aa25b3e56e4.vivox.com/version_morpheus
                parts = key.split("/")

                # parts[0] = "" (before first /)
                # parts[1] = "discovery"
                # parts[2] = customer/domain (e.g., "bnxp")
                # parts[3] = role (e.g., "mphpp")
                # parts[4] = hostname (e.g., "mphpp-bnxp-010103-1-3f6e3aa25b3e56e4.vivox.com")
                # parts[5] = key_type (e.g., "version_morpheus", "viv_privip")

                if len(parts) < 5:
                    continue

                domain = parts[2]
                role = parts[3]
                hostname = parts[4]

                # Skip if hostname doesn't look valid
                if not hostname or "." not in hostname:
                    continue

                roles.add(role)
                domains.add(domain)

                # Only add each hostname once (there are multiple keys per host)
                if hostname not in hosts:
                    hosts[hostname] = {
                        "hostname": hostname,
                        "role": role,
                        "customer": domain,  # 'customer' for backward compatibility
                        "domain": domain,
                    }

            client.close()
            return hosts, domains, roles

        except Exception as e:
            logger.error(f"Error fetching from etcd: {e}")
            import traceback
            logger.error(traceback.format_exc())
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
