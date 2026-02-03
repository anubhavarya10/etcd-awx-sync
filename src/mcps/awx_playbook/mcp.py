"""MCP implementation for AWX playbook operations."""

import os
import asyncio
import time
import logging
import requests
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor

from ..base import BaseMCP, MCPAction, MCPResult, MCPResultStatus

logger = logging.getLogger(__name__)

# Thread pool for running operations
_executor = ThreadPoolExecutor(max_workers=4)


class AwxPlaybookMCP(BaseMCP):
    """
    MCP for AWX playbook operations.

    This MCP provides actions to:
    - List playbooks from GitHub
    - Run playbooks on inventories
    - Check job status
    - View job output
    """

    def __init__(self):
        # AWX configuration
        self.awx_server = os.environ.get("AWX_SERVER", "localhost")
        self.awx_username = os.environ.get("AWX_USERNAME")
        self.awx_password = os.environ.get("AWX_PASSWORD")

        # GitHub configuration
        self.github_api_url = os.environ.get(
            "GITHUB_API_URL",
            "https://github.cds.internal.unity3d.com/api/v3"
        )
        self.github_token = os.environ.get("GITHUB_TOKEN")
        self.github_repo = os.environ.get("GITHUB_PLAYBOOKS_REPO", "unity/vivox-ops-docker")
        self.github_path = os.environ.get("GITHUB_PLAYBOOKS_PATH", "ansible")
        self.github_branch = os.environ.get("GITHUB_PLAYBOOKS_BRANCH", "main")

        # AWX credential ID for SSH access to hosts
        self.awx_credential_id = os.environ.get("AWX_CREDENTIAL_ID")

        # Cache for playbooks
        self._playbook_cache: Dict[str, Any] = {
            "playbooks": [],
            "last_refresh": 0,
        }
        self._cache_ttl = 300  # 5 minutes

        # Track AWX project ID (created once)
        self._awx_project_id: Optional[int] = None

        super().__init__()

    @property
    def name(self) -> str:
        return "awx-playbook"

    @property
    def description(self) -> str:
        return "Run Ansible playbooks on AWX inventories"

    def _setup_actions(self) -> None:
        """Register available actions."""

        self.register_action(MCPAction(
            name="list-playbooks",
            description="List available playbooks from GitHub",
            parameters=[],
            requires_confirmation=False,
            examples=[
                "list playbooks",
                "show available playbooks",
                "what playbooks are available",
            ],
        ))

        self.register_action(MCPAction(
            name="run-playbook",
            description="Run a playbook on an inventory",
            parameters=[
                {
                    "name": "playbook",
                    "type": "string",
                    "description": "Name of the playbook (e.g., deploy-app.yml)",
                    "required": True,
                },
                {
                    "name": "inventory",
                    "type": "string",
                    "description": "Name of the AWX inventory to run against",
                    "required": True,
                },
                {
                    "name": "extra_vars",
                    "type": "string",
                    "description": "Extra variables in JSON format",
                    "required": False,
                },
            ],
            requires_confirmation=True,
            examples=[
                "run playbook deploy-app.yml on mim-nwxp",
                "run restart-services on mphpp-pubwxp",
                "execute update-config.yml on ts-valxp",
            ],
        ))

        self.register_action(MCPAction(
            name="job-status",
            description="Check the status of an AWX job",
            parameters=[
                {
                    "name": "job_id",
                    "type": "integer",
                    "description": "AWX job ID",
                    "required": True,
                },
            ],
            requires_confirmation=False,
            examples=[
                "job status 123",
                "check job 456",
                "status of job 789",
            ],
        ))

        self.register_action(MCPAction(
            name="job-output",
            description="Show the output of an AWX job",
            parameters=[
                {
                    "name": "job_id",
                    "type": "integer",
                    "description": "AWX job ID",
                    "required": True,
                },
                {
                    "name": "lines",
                    "type": "integer",
                    "description": "Number of lines to show (default: 50)",
                    "required": False,
                },
            ],
            requires_confirmation=False,
            examples=[
                "job output 123",
                "show output of job 456",
                "get logs for job 789",
            ],
        ))

        self.register_action(MCPAction(
            name="list-jobs",
            description="List recent AWX jobs",
            parameters=[
                {
                    "name": "limit",
                    "type": "integer",
                    "description": "Number of jobs to show (default: 10)",
                    "required": False,
                },
            ],
            requires_confirmation=False,
            examples=[
                "list jobs",
                "show recent jobs",
                "list last 5 jobs",
            ],
        ))

        self.register_action(MCPAction(
            name="set-repo",
            description="Change the GitHub repository for playbooks",
            parameters=[
                {
                    "name": "repo",
                    "type": "string",
                    "description": "GitHub repository (e.g., org/repo-name)",
                    "required": True,
                },
                {
                    "name": "path",
                    "type": "string",
                    "description": "Path to playbooks folder (default: ansible)",
                    "required": False,
                },
                {
                    "name": "branch",
                    "type": "string",
                    "description": "Branch name (default: main)",
                    "required": False,
                },
            ],
            requires_confirmation=False,
            examples=[
                "set repo unity/vivox-ops-docker",
                "set repo unity/ansible-playbooks path playbooks",
                "change repo to org/new-repo branch develop",
            ],
        ))

        self.register_action(MCPAction(
            name="show-repo",
            description="Show the current playbook repository configuration",
            parameters=[],
            requires_confirmation=False,
            examples=[
                "show repo",
                "current repo",
                "playbook repo config",
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

        if action == "list-playbooks":
            return await self._handle_list_playbooks()
        elif action == "run-playbook":
            return await self._handle_run_playbook(parameters, user_id, channel_id)
        elif action == "job-status":
            return await self._handle_job_status(parameters)
        elif action == "job-output":
            return await self._handle_job_output(parameters)
        elif action == "list-jobs":
            return await self._handle_list_jobs(parameters)
        elif action == "set-repo":
            return await self._handle_set_repo(parameters)
        elif action == "show-repo":
            return await self._handle_show_repo()
        else:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Unknown action: {action}"
            )

    async def _handle_list_playbooks(self) -> MCPResult:
        """List available playbooks from GitHub."""
        try:
            playbooks = await self._fetch_playbooks_from_github()

            if not playbooks:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=(
                        "⚠️ *No playbooks found*\n\n"
                        f"Repository: `{self.github_repo}`\n"
                        f"Path: `{self.github_path}`\n\n"
                        "_Ready for next task_"
                    )
                )

            # Auto-create/sync AWX project so playbooks appear in AWX GUI
            project_id = await self._ensure_awx_project()
            project_status = ""
            if project_id:
                # Trigger project sync
                await self._sync_awx_project(project_id)
                project_status = f"\n*AWX Project:* Synced (ID: {project_id})"
            else:
                project_status = "\n*AWX Project:* ⚠️ Could not create (check AWX credentials)"

            lines = ["📚 *Available Playbooks*\n"]
            for pb in playbooks:
                name = pb.get("name", "unknown")
                # Remove .yml extension for display
                display_name = name.replace(".yml", "").replace(".yaml", "")
                lines.append(f"  • `{display_name}`")

            lines.append(f"\n*Total:* {len(playbooks)} playbooks")
            lines.append(f"*Source:* `{self.github_repo}/{self.github_path}`")
            lines.append(project_status)
            lines.append("\n_To run: `/agent run playbook <name> on <inventory>`_")
            lines.append("\n_Ready for next task_")

            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message="\n".join(lines),
                data={"playbooks": playbooks, "project_id": project_id}
            )

        except Exception as e:
            logger.exception("Error listing playbooks")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error listing playbooks:* {str(e)}\n\n_Ready for next task_"
            )

    async def _handle_run_playbook(
        self,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Handle running a playbook on an inventory."""
        playbook = parameters.get("playbook", "")
        inventory = parameters.get("inventory", "")
        extra_vars = parameters.get("extra_vars")

        # Normalize playbook name
        if not playbook.endswith((".yml", ".yaml")):
            playbook = f"{playbook}.yml"

        # Validate playbook exists
        playbooks = await self._fetch_playbooks_from_github()
        playbook_names = [p.get("name", "").lower() for p in playbooks]

        if playbook.lower() not in playbook_names:
            suggestions = [p.get("name", "") for p in playbooks[:5]]
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"⚠️ *Unknown playbook:* `{playbook}`\n\n"
                    f"*Available playbooks:* {', '.join(f'`{s}`' for s in suggestions)}\n\n"
                    "Use `list playbooks` to see all.\n\n"
                    "_Ready for next task_"
                )
            )

        # Validate inventory exists
        inventory_info = await self._get_inventory(inventory)
        if not inventory_info:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"⚠️ *Unknown inventory:* `{inventory}`\n\n"
                    "Use `list domains` or `list roles` to see available inventories.\n\n"
                    "_Ready for next task_"
                )
            )

        inv_id = inventory_info.get("id")
        host_count = inventory_info.get("total_hosts", 0)

        # Create confirmation
        return self.create_confirmation(
            action="run-playbook",
            parameters={
                "playbook": playbook,
                "inventory": inventory,
                "inventory_id": inv_id,
                "extra_vars": extra_vars,
            },
            user_id=user_id,
            channel_id=channel_id,
            confirmation_message=(
                f"🚀 *Confirm Playbook Execution*\n\n"
                f"*Playbook:* `{playbook}`\n"
                f"*Inventory:* `{inventory}` ({host_count} hosts)\n"
                f"*Extra vars:* `{extra_vars or 'none'}`\n\n"
                f"⚠️ This will execute the playbook on *{host_count}* hosts.\n\n"
                f"Do you want to proceed?"
            ),
        )

    async def _execute_confirmed(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Execute action after user confirmation."""
        if action == "run-playbook":
            return await self._execute_playbook(
                playbook=parameters.get("playbook"),
                inventory_id=parameters.get("inventory_id"),
                inventory_name=parameters.get("inventory"),
                extra_vars=parameters.get("extra_vars"),
            )
        else:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Unknown confirmed action: {action}"
            )

    async def _execute_playbook(
        self,
        playbook: str,
        inventory_id: int,
        inventory_name: str,
        extra_vars: Optional[str] = None,
    ) -> MCPResult:
        """Execute a playbook on AWX."""
        try:
            # Step 1: Ensure AWX project exists
            project_id = await self._ensure_awx_project()
            if not project_id:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ *Failed to create AWX project*\n\n_Ready for next task_"
                )

            # Step 2: Create or get job template
            template_id = await self._ensure_job_template(
                playbook=playbook,
                project_id=project_id,
                inventory_id=inventory_id,
            )
            if not template_id:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ *Failed to create job template*\n\n_Ready for next task_"
                )

            # Step 3: Launch the job
            job = await self._launch_job(template_id, extra_vars)
            if not job:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ *Failed to launch job*\n\n_Ready for next task_"
                )

            job_id = job.get("id")
            job_url = f"http://{self.awx_server}/#/jobs/playbook/{job_id}"

            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=(
                    f"✅ *Job Launched*\n\n"
                    f"*Job ID:* `{job_id}`\n"
                    f"*Playbook:* `{playbook}`\n"
                    f"*Inventory:* `{inventory_name}`\n"
                    f"*Status:* `{job.get('status', 'pending')}`\n\n"
                    f"*View in AWX:* <{job_url}|Open Job>\n\n"
                    f"To check status: `job status {job_id}`\n"
                    f"To view output: `job output {job_id}`\n\n"
                    f"_Ready for next task_"
                ),
                data={"job_id": job_id, "job": job}
            )

        except Exception as e:
            logger.exception("Error executing playbook")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error executing playbook:* {str(e)}\n\n_Ready for next task_"
            )

    async def _handle_job_status(self, parameters: Dict[str, Any]) -> MCPResult:
        """Get status of an AWX job."""
        job_id = parameters.get("job_id")

        if not job_id:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="⚠️ *Job ID required*\n\nUsage: `job status <job_id>`\n\n_Ready for next task_"
            )

        try:
            job = await self._get_job(job_id)
            if not job:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=f"⚠️ *Job not found:* `{job_id}`\n\n_Ready for next task_"
                )

            status = job.get("status", "unknown")
            status_emoji = self._get_status_emoji(status)

            # Calculate duration
            started = job.get("started")
            finished = job.get("finished")
            duration = ""
            if started and finished:
                # Parse and calculate (simplified)
                duration = f"\n*Duration:* {job.get('elapsed', 'N/A')}s"

            job_url = f"http://{self.awx_server}/#/jobs/playbook/{job_id}"

            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=(
                    f"{status_emoji} *Job Status*\n\n"
                    f"*Job ID:* `{job_id}`\n"
                    f"*Status:* `{status}`\n"
                    f"*Playbook:* `{job.get('playbook', 'N/A')}`"
                    f"{duration}\n\n"
                    f"*View in AWX:* <{job_url}|Open Job>\n\n"
                    f"_Ready for next task_"
                ),
                data={"job": job}
            )

        except Exception as e:
            logger.exception("Error getting job status")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error getting job status:* {str(e)}\n\n_Ready for next task_"
            )

    async def _handle_job_output(self, parameters: Dict[str, Any]) -> MCPResult:
        """Get output of an AWX job."""
        job_id = parameters.get("job_id")
        lines = parameters.get("lines", 50)

        if not job_id:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="⚠️ *Job ID required*\n\nUsage: `job output <job_id>`\n\n_Ready for next task_"
            )

        try:
            output = await self._get_job_output(job_id, lines)
            if output is None:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=f"⚠️ *Job not found:* `{job_id}`\n\n_Ready for next task_"
                )

            # Truncate if too long for Slack
            if len(output) > 3000:
                output = output[:3000] + "\n... (truncated)"

            job_url = f"http://{self.awx_server}/#/jobs/playbook/{job_id}"

            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=(
                    f"📄 *Job Output* (Job `{job_id}`)\n\n"
                    f"```\n{output}\n```\n\n"
                    f"*Full output:* <{job_url}|View in AWX>\n\n"
                    f"_Ready for next task_"
                ),
            )

        except Exception as e:
            logger.exception("Error getting job output")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error getting job output:* {str(e)}\n\n_Ready for next task_"
            )

    async def _handle_list_jobs(self, parameters: Dict[str, Any]) -> MCPResult:
        """List recent AWX jobs."""
        limit = parameters.get("limit", 10)

        try:
            jobs = await self._list_jobs(limit)

            if not jobs:
                return MCPResult(
                    status=MCPResultStatus.SUCCESS,
                    message="📋 *No recent jobs found*\n\n_Ready for next task_"
                )

            lines = ["📋 *Recent Jobs*\n"]
            for job in jobs:
                job_id = job.get("id")
                status = job.get("status", "unknown")
                status_emoji = self._get_status_emoji(status)
                name = job.get("name", "unknown")
                lines.append(f"  {status_emoji} `{job_id}` - {name} ({status})")

            lines.append(f"\n*Total:* {len(jobs)} jobs shown")
            lines.append("\n_Ready for next task_")

            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message="\n".join(lines),
                data={"jobs": jobs}
            )

        except Exception as e:
            logger.exception("Error listing jobs")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error listing jobs:* {str(e)}\n\n_Ready for next task_"
            )

    async def _handle_set_repo(self, parameters: Dict[str, Any]) -> MCPResult:
        """Set the GitHub repository for playbooks."""
        repo = parameters.get("repo", "").strip()
        path = parameters.get("path", "").strip()
        branch = parameters.get("branch", "").strip()

        if not repo:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    "⚠️ *Repository required*\n\n"
                    "Usage: `set repo <org/repo-name> [path <folder>] [branch <branch>]`\n\n"
                    "Example: `set repo unity/ansible-playbooks path playbooks branch main`\n\n"
                    "_Ready for next task_"
                )
            )

        # Validate repo format (should be org/repo)
        if "/" not in repo:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"⚠️ *Invalid repository format:* `{repo}`\n\n"
                    "Expected format: `org/repo-name`\n"
                    "Example: `unity/vivox-ops-docker`\n\n"
                    "_Ready for next task_"
                )
            )

        old_repo = self.github_repo
        old_path = self.github_path
        old_branch = self.github_branch

        # Update configuration
        self.github_repo = repo
        if path:
            self.github_path = path
        if branch:
            self.github_branch = branch

        # Clear cache to force refresh
        self._playbook_cache = {"playbooks": [], "last_refresh": 0}
        # Reset AWX project ID since repo changed
        self._awx_project_id = None

        # Try to fetch playbooks from new repo to validate
        try:
            playbooks = await self._fetch_playbooks_from_github()

            if not playbooks:
                # Revert changes
                self.github_repo = old_repo
                self.github_path = old_path
                self.github_branch = old_branch
                self._playbook_cache = {"playbooks": [], "last_refresh": 0}

                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=(
                        f"⚠️ *No playbooks found in new repo*\n\n"
                        f"Repository: `{repo}`\n"
                        f"Path: `{path or self.github_path}`\n"
                        f"Branch: `{branch or self.github_branch}`\n\n"
                        "Configuration not changed.\n\n"
                        "_Ready for next task_"
                    )
                )

            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=(
                    f"✅ *Repository Updated*\n\n"
                    f"*Repository:* `{self.github_repo}`\n"
                    f"*Path:* `{self.github_path}`\n"
                    f"*Branch:* `{self.github_branch}`\n\n"
                    f"*Playbooks found:* {len(playbooks)}\n\n"
                    "_Ready for next task_"
                ),
                data={"repo": self.github_repo, "path": self.github_path, "branch": self.github_branch}
            )

        except Exception as e:
            # Revert changes on error
            self.github_repo = old_repo
            self.github_path = old_path
            self.github_branch = old_branch
            self._playbook_cache = {"playbooks": [], "last_refresh": 0}

            logger.exception("Error setting repo")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"❌ *Error accessing repository:* {str(e)}\n\n"
                    "Configuration not changed.\n\n"
                    "_Ready for next task_"
                )
            )

    async def _handle_show_repo(self) -> MCPResult:
        """Show current playbook repository configuration."""
        # Get playbook count from cache or fetch
        playbook_count = len(self._playbook_cache.get("playbooks", []))
        if playbook_count == 0:
            try:
                playbooks = await self._fetch_playbooks_from_github()
                playbook_count = len(playbooks)
            except Exception:
                playbook_count = 0

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=(
                f"📁 *Playbook Repository Configuration*\n\n"
                f"*Repository:* `{self.github_repo}`\n"
                f"*Path:* `{self.github_path}`\n"
                f"*Branch:* `{self.github_branch}`\n"
                f"*API URL:* `{self.github_api_url}`\n\n"
                f"*Playbooks available:* {playbook_count}\n\n"
                f"To change: `set repo <org/repo> [path <folder>] [branch <branch>]`\n\n"
                f"_Ready for next task_"
            ),
            data={
                "repo": self.github_repo,
                "path": self.github_path,
                "branch": self.github_branch,
                "api_url": self.github_api_url,
                "playbook_count": playbook_count,
            }
        )

    # Helper methods

    def _get_status_emoji(self, status: str) -> str:
        """Get emoji for job status."""
        status_emojis = {
            "successful": "✅",
            "failed": "❌",
            "running": "🔄",
            "pending": "⏳",
            "waiting": "⏳",
            "canceled": "🚫",
            "error": "❌",
        }
        return status_emojis.get(status.lower(), "❓")

    async def _fetch_playbooks_from_github(self) -> List[Dict[str, Any]]:
        """Fetch list of playbooks from GitHub."""
        # Check cache
        if (time.time() - self._playbook_cache["last_refresh"]) < self._cache_ttl:
            if self._playbook_cache["playbooks"]:
                return self._playbook_cache["playbooks"]

        try:
            loop = asyncio.get_event_loop()
            playbooks = await loop.run_in_executor(
                _executor,
                self._fetch_playbooks_sync,
            )
            self._playbook_cache["playbooks"] = playbooks
            self._playbook_cache["last_refresh"] = time.time()
            return playbooks
        except Exception as e:
            logger.error(f"Error fetching playbooks: {e}")
            return []

    def _fetch_playbooks_sync(self) -> List[Dict[str, Any]]:
        """Synchronously fetch playbooks from GitHub API."""
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        # Get contents of the ansible directory
        url = f"{self.github_api_url}/repos/{self.github_repo}/contents/{self.github_path}"
        params = {"ref": self.github_branch}

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()

        contents = response.json()
        playbooks = []

        for item in contents:
            name = item.get("name", "")
            if name.endswith((".yml", ".yaml")) and item.get("type") == "file":
                playbooks.append({
                    "name": name,
                    "path": item.get("path"),
                    "sha": item.get("sha"),
                    "url": item.get("html_url"),
                })

        return playbooks

    async def _get_inventory(self, name: str) -> Optional[Dict[str, Any]]:
        """Get inventory by name from AWX."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._get_inventory_sync,
                name,
            )
        except Exception as e:
            logger.error(f"Error getting inventory: {e}")
            return None

    def _get_inventory_sync(self, name: str) -> Optional[Dict[str, Any]]:
        """Synchronously get inventory from AWX."""
        url = f"http://{self.awx_server}/api/v2/inventories/"
        response = requests.get(
            url,
            params={"name": name},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        return results[0] if results else None

    async def _ensure_awx_project(self) -> Optional[int]:
        """Ensure AWX project exists for the GitHub repo."""
        if self._awx_project_id:
            return self._awx_project_id

        try:
            loop = asyncio.get_event_loop()
            project_id = await loop.run_in_executor(
                _executor,
                self._ensure_awx_project_sync,
            )
            self._awx_project_id = project_id
            return project_id
        except Exception as e:
            logger.error(f"Error ensuring AWX project: {e}")
            return None

    def _ensure_awx_project_sync(self) -> Optional[int]:
        """Synchronously ensure AWX project exists."""
        project_name = f"slack-bot-{self.github_repo.replace('/', '-')}"

        # Check if project exists
        url = f"http://{self.awx_server}/api/v2/projects/"
        response = requests.get(
            url,
            params={"name": project_name},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        if results:
            project = results[0]
            project_id = project["id"]

            # Check if project has a credential, if not add one
            if not project.get("credential") and self.github_token:
                # Get org ID from project
                org_id = project.get("organization", 1)
                scm_credential_id = self._ensure_scm_credential(org_id)
                if scm_credential_id:
                    # Update project with credential
                    update_url = f"http://{self.awx_server}/api/v2/projects/{project_id}/"
                    requests.patch(
                        update_url,
                        json={"credential": scm_credential_id},
                        auth=(self.awx_username, self.awx_password),
                        timeout=30
                    )
                    logger.info(f"Updated project {project_id} with SCM credential {scm_credential_id}")

            return project_id

        # Get organization ID
        org_url = f"http://{self.awx_server}/api/v2/organizations/"
        org_response = requests.get(
            org_url,
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        org_response.raise_for_status()
        org_id = org_response.json().get("results", [{}])[0].get("id", 1)

        # Create or get SCM credential for GitHub
        scm_credential_id = None
        if self.github_token:
            scm_credential_id = self._ensure_scm_credential(org_id)

        # Create project
        # Build the full GitHub URL for SCM
        github_base = self.github_api_url.replace("/api/v3", "")
        scm_url = f"{github_base}/{self.github_repo}.git"

        payload = {
            "name": project_name,
            "description": f"Auto-created by Slack bot for {self.github_repo}",
            "organization": org_id,
            "scm_type": "git",
            "scm_url": scm_url,
            "scm_branch": self.github_branch,
            "scm_update_on_launch": True,
        }

        # Add SCM credential if available
        if scm_credential_id:
            payload["credential"] = scm_credential_id

        response = requests.post(
            url,
            json=payload,
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        return response.json().get("id")

    def _ensure_scm_credential(self, org_id: int) -> Optional[int]:
        """Create or get SCM credential for GitHub authentication."""
        credential_name = "slack-bot-github-token"

        # Check if credential exists
        url = f"http://{self.awx_server}/api/v2/credentials/"
        response = requests.get(
            url,
            params={"name": credential_name},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        if results:
            # Update existing credential with current token
            cred_id = results[0]["id"]
            update_url = f"http://{self.awx_server}/api/v2/credentials/{cred_id}/"
            requests.patch(
                update_url,
                json={"inputs": {"password": self.github_token}},
                auth=(self.awx_username, self.awx_password),
                timeout=30
            )
            return cred_id

        # Get credential type ID for "Source Control"
        cred_type_url = f"http://{self.awx_server}/api/v2/credential_types/"
        cred_type_response = requests.get(
            cred_type_url,
            params={"name": "Source Control"},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        cred_type_response.raise_for_status()
        cred_types = cred_type_response.json().get("results", [])
        if not cred_types:
            logger.error("Could not find 'Source Control' credential type")
            return None
        cred_type_id = cred_types[0]["id"]

        # Create credential
        payload = {
            "name": credential_name,
            "description": "Auto-created by Slack bot for GitHub access",
            "organization": org_id,
            "credential_type": cred_type_id,
            "inputs": {
                "username": "git",
                "password": self.github_token,
            },
        }

        response = requests.post(
            url,
            json=payload,
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        return response.json().get("id")

    async def _sync_awx_project(self, project_id: int) -> bool:
        """Trigger a sync of the AWX project."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._sync_awx_project_sync,
                project_id,
            )
        except Exception as e:
            logger.error(f"Error syncing AWX project: {e}")
            return False

    def _sync_awx_project_sync(self, project_id: int) -> bool:
        """Synchronously trigger a project sync."""
        url = f"http://{self.awx_server}/api/v2/projects/{project_id}/update/"
        try:
            response = requests.post(
                url,
                auth=(self.awx_username, self.awx_password),
                timeout=30
            )
            # 202 Accepted is success for async operations
            return response.status_code in [200, 201, 202]
        except Exception as e:
            logger.error(f"Error syncing project: {e}")
            return False

    async def _ensure_job_template(
        self,
        playbook: str,
        project_id: int,
        inventory_id: int,
    ) -> Optional[int]:
        """Ensure job template exists for the playbook."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._ensure_job_template_sync,
                playbook,
                project_id,
                inventory_id,
            )
        except Exception as e:
            logger.error(f"Error ensuring job template: {e}")
            return None

    def _ensure_job_template_sync(
        self,
        playbook: str,
        project_id: int,
        inventory_id: int,
    ) -> Optional[int]:
        """Synchronously ensure job template exists."""
        # Template name based on playbook
        template_name = f"slack-bot-{playbook.replace('.yml', '').replace('.yaml', '')}"

        # Check if template exists
        url = f"http://{self.awx_server}/api/v2/job_templates/"
        response = requests.get(
            url,
            params={"name": template_name},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        if results:
            template = results[0]
            # Update inventory if different
            if template.get("inventory") != inventory_id:
                update_url = f"http://{self.awx_server}/api/v2/job_templates/{template['id']}/"
                requests.patch(
                    update_url,
                    json={"inventory": inventory_id},
                    auth=(self.awx_username, self.awx_password),
                    timeout=30
                )
            return template["id"]

        # Create template
        playbook_path = f"{self.github_path}/{playbook}"

        payload = {
            "name": template_name,
            "description": f"Auto-created by Slack bot for {playbook}",
            "project": project_id,
            "playbook": playbook_path,
            "inventory": inventory_id,
            "job_type": "run",
            "ask_variables_on_launch": True,
        }

        # Add credential if configured
        if self.awx_credential_id:
            payload["credential"] = int(self.awx_credential_id)

        response = requests.post(
            url,
            json=payload,
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        return response.json().get("id")

    async def _launch_job(
        self,
        template_id: int,
        extra_vars: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Launch a job from a template."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._launch_job_sync,
                template_id,
                extra_vars,
            )
        except Exception as e:
            logger.error(f"Error launching job: {e}")
            return None

    def _launch_job_sync(
        self,
        template_id: int,
        extra_vars: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Synchronously launch a job."""
        url = f"http://{self.awx_server}/api/v2/job_templates/{template_id}/launch/"

        payload = {}
        if extra_vars:
            payload["extra_vars"] = extra_vars

        response = requests.post(
            url,
            json=payload,
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        return response.json()

    async def _get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Get job details."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._get_job_sync,
                job_id,
            )
        except Exception as e:
            logger.error(f"Error getting job: {e}")
            return None

    def _get_job_sync(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Synchronously get job details."""
        url = f"http://{self.awx_server}/api/v2/jobs/{job_id}/"

        response = requests.get(
            url,
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        return response.json()

    async def _get_job_output(self, job_id: int, lines: int = 50) -> Optional[str]:
        """Get job output."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._get_job_output_sync,
                job_id,
                lines,
            )
        except Exception as e:
            logger.error(f"Error getting job output: {e}")
            return None

    def _get_job_output_sync(self, job_id: int, lines: int = 50) -> Optional[str]:
        """Synchronously get job output."""
        url = f"http://{self.awx_server}/api/v2/jobs/{job_id}/stdout/"

        response = requests.get(
            url,
            params={"format": "txt"},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )

        if response.status_code == 404:
            return None

        response.raise_for_status()

        # Get last N lines
        output = response.text
        output_lines = output.split("\n")
        if len(output_lines) > lines:
            output = "\n".join(output_lines[-lines:])

        return output

    async def _list_jobs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """List recent jobs."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._list_jobs_sync,
                limit,
            )
        except Exception as e:
            logger.error(f"Error listing jobs: {e}")
            return []

    def _list_jobs_sync(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Synchronously list recent jobs."""
        url = f"http://{self.awx_server}/api/v2/jobs/"

        response = requests.get(
            url,
            params={"order_by": "-created", "page_size": limit},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        return response.json().get("results", [])

    async def health_check(self) -> bool:
        """Check if AWX and GitHub are reachable."""
        try:
            # Check AWX
            response = requests.get(
                f"http://{self.awx_server}/api/v2/ping/",
                auth=(self.awx_username, self.awx_password),
                timeout=10
            )
            return response.status_code == 200
        except Exception:
            return False
