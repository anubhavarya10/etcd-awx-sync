"""MCP implementation for AWX playbook operations."""

import os
import asyncio
import time
import logging
import requests
from typing import Any, Dict, List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor

from ..base import BaseMCP, MCPAction, MCPResult, MCPResultStatus
from ...request_queue import (
    RequestQueue, PlaybookRequest, RequestPriority, RequestStatus, get_queue
)

logger = logging.getLogger(__name__)

# Thread pool for running operations
_executor = ThreadPoolExecutor(max_workers=4)


class AwxPlaybookMCP(BaseMCP):
    """
    MCP for AWX playbook operations.

    This MCP provides actions to:
    - List playbooks from GitHub
    - Run playbooks on inventories (with queue management)
    - Check job status
    - View job output
    - Manage request queue
    """

    def __init__(self, notify_callback: Optional[Callable] = None):
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

        # Request queue for multi-user handling
        self._notify_callback = notify_callback
        self._queue: Optional[RequestQueue] = None
        self._queue_enabled = os.environ.get("ENABLE_QUEUE", "true").lower() == "true"

        super().__init__()

    async def initialize_queue(self, notify_callback: Callable):
        """Initialize the request queue with a notification callback."""
        self._notify_callback = notify_callback
        self._queue = get_queue()
        self._queue.notify_callback = notify_callback

        # Set up executor that calls our internal method
        async def queue_executor(playbook, inventory, extra_vars, user_id, channel_id):
            return await self._execute_playbook_internal(
                playbook=playbook,
                inventory=inventory,
                extra_vars=extra_vars,
            )

        await self._queue.start(queue_executor)
        logger.info("AWX Playbook queue initialized")

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

        self.register_action(MCPAction(
            name="show-playbook",
            description="Show files inside a playbook folder",
            parameters=[
                {
                    "name": "playbook",
                    "type": "string",
                    "description": "Name of the playbook folder to inspect",
                    "required": True,
                },
            ],
            requires_confirmation=False,
            examples=[
                "show playbook check-service",
                "what's inside check-service",
                "list files in harjo-setup",
            ],
        ))

        self.register_action(MCPAction(
            name="setup-ssh",
            description="Check or setup SSH credential for AWX",
            parameters=[],
            requires_confirmation=False,
            examples=[
                "setup ssh",
                "check ssh credential",
                "awx ssh status",
            ],
        ))

        # Queue management actions
        self.register_action(MCPAction(
            name="queue-status",
            description="Show the current request queue status",
            parameters=[],
            requires_confirmation=False,
            examples=[
                "queue status",
                "show queue",
                "what's running",
            ],
        ))

        self.register_action(MCPAction(
            name="my-requests",
            description="Show your recent requests",
            parameters=[],
            requires_confirmation=False,
            examples=[
                "my requests",
                "my jobs",
                "show my requests",
            ],
        ))

        self.register_action(MCPAction(
            name="cancel-request",
            description="Cancel a pending request",
            parameters=[
                {
                    "name": "request_id",
                    "type": "string",
                    "description": "Request ID to cancel",
                    "required": True,
                },
            ],
            requires_confirmation=False,
            examples=[
                "cancel request abc123",
                "cancel abc123",
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
        elif action == "show-playbook":
            return await self._handle_show_playbook(parameters)
        elif action == "setup-ssh":
            return await self._handle_setup_ssh()
        elif action == "queue-status":
            return await self._handle_queue_status()
        elif action == "my-requests":
            return await self._handle_my_requests(user_id)
        elif action == "cancel-request":
            return await self._handle_cancel_request(parameters, user_id)
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
            folders = []
            files = []

            for pb in playbooks:
                name = pb.get("name", "unknown")
                pb_type = pb.get("type", "file")
                if pb_type == "folder":
                    folders.append(name)
                else:
                    # Remove .yml extension for display
                    display_name = name.replace(".yml", "").replace(".yaml", "")
                    files.append(display_name)

            if folders:
                lines.append("*📂 Playbook Folders* (contain multiple files):")
                for f in sorted(folders):
                    lines.append(f"  • `{f}/` - use `show playbook {f}` to see files")

            if files:
                if folders:
                    lines.append("\n*📄 Standalone Playbooks:*")
                for f in sorted(files):
                    lines.append(f"  • `{f}`")

            lines.append(f"\n*Total:* {len(folders)} folders, {len(files)} files")
            lines.append(f"*Source:* `{self.github_repo}/{self.github_path}`")
            lines.append(project_status)
            lines.append("\n_To inspect folder: `show playbook <name>`_")
            lines.append("_To run: `run playbook <folder>/<file>.yml on <inventory>`_")
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

        # Handle playbook path resolution
        # User might say: "check-service" or "check-service/check-service.yml"
        playbook_path = playbook
        playbook_display = playbook

        # Fetch available playbooks
        playbooks = await self._fetch_playbooks_from_github()

        # Check if it's a folder
        folder_match = None
        file_match = None

        for pb in playbooks:
            name = pb.get("name", "")
            pb_type = pb.get("type", "file")

            if pb_type == "folder":
                # Check if user provided folder name (with or without trailing slash)
                if playbook.lower().rstrip("/") == name.lower():
                    folder_match = name
                    break
            else:
                # Check if it's a direct file match
                name_lower = name.lower()
                playbook_lower = playbook.lower()
                if playbook_lower == name_lower or playbook_lower + ".yml" == name_lower:
                    file_match = name
                    break

        if folder_match:
            # It's a folder - look for main playbook inside
            files = await self._fetch_playbook_files(folder_match)
            yml_files = [f for f in files if f.get("name", "").endswith((".yml", ".yaml"))]

            if not yml_files:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=(
                        f"⚠️ *No playbooks found in folder:* `{folder_match}/`\n\n"
                        "Use `show playbook {folder_match}` to see contents.\n\n"
                        "_Ready for next task_"
                    )
                )

            if len(yml_files) == 1:
                # Only one playbook - use it
                playbook_path = f"{folder_match}/{yml_files[0]['name']}"
                playbook_display = playbook_path
            else:
                # Multiple playbooks - try to find the best match
                main_playbook = None
                yml_names = [f.get("name", "") for f in yml_files]

                # Priority 1: File matching folder name (e.g., morpheus-mphpp-setup.yml)
                for name in yml_names:
                    if name.replace(".yml", "").replace(".yaml", "") == folder_match:
                        main_playbook = name
                        break

                # Priority 2: Common main entry points
                if not main_playbook:
                    for common_name in ["main.yml", "site.yml", "playbook.yml", "setup.yml"]:
                        if common_name in yml_names:
                            main_playbook = common_name
                            break

                if main_playbook:
                    playbook_path = f"{folder_match}/{main_playbook}"
                    playbook_display = playbook_path
                else:
                    # Can't determine - ask user to specify
                    file_list = "\n".join([f"  • `{folder_match}/{f['name']}`" for f in yml_files])
                    return MCPResult(
                        status=MCPResultStatus.ERROR,
                        message=(
                            f"📂 *Multiple playbooks in folder:* `{folder_match}/`\n\n"
                            f"{file_list}\n\n"
                            f"Please specify which one:\n"
                            f"`run playbook {folder_match}/<filename>.yml on {inventory}`\n\n"
                            "_Ready for next task_"
                        )
                    )
        elif file_match:
            playbook_path = file_match
            playbook_display = file_match
        elif "/" in playbook:
            # User specified full path like "check-service/check-service.yml"
            playbook_path = playbook
            if not playbook_path.endswith((".yml", ".yaml")):
                playbook_path = f"{playbook_path}.yml"
            playbook_display = playbook_path
        else:
            # Not found
            suggestions = [p.get("name", "") for p in playbooks[:5]]
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"⚠️ *Unknown playbook:* `{playbook}`\n\n"
                    f"*Available:* {', '.join(f'`{s}`' for s in suggestions)}\n\n"
                    "Use `list playbooks` to see all.\n"
                    "Use `show playbook <name>` to see folder contents.\n\n"
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

        # Check if queue is enabled and initialized
        if self._queue_enabled and self._queue:
            # Submit to queue for multi-user handling
            # Parse extra_vars if provided
            extra_vars_dict = {}
            if extra_vars:
                try:
                    import json
                    extra_vars_dict = json.loads(extra_vars) if isinstance(extra_vars, str) else extra_vars
                except json.JSONDecodeError:
                    pass

            request = PlaybookRequest.create(
                user_id=user_id,
                user_name=user_id,  # Will be user ID, Slack can resolve to name later
                channel_id=channel_id,
                playbook=playbook_path,
                inventory=inventory,
                extra_vars=extra_vars_dict,
                priority=RequestPriority.NORMAL,
            )

            success, message = await self._queue.submit(request)

            return MCPResult(
                status=MCPResultStatus.SUCCESS if success else MCPResultStatus.ERROR,
                message=message,
                data={"request_id": request.id if success else None}
            )

        # Run immediately without queue (direct execution)
        return await self._execute_playbook_with_streaming(
            playbook=playbook_path,
            playbook_display=playbook_display,
            inventory_id=inv_id,
            inventory_name=inventory,
            host_count=host_count,
            extra_vars=extra_vars,
            channel_id=channel_id,
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

    async def _execute_playbook_with_streaming(
        self,
        playbook: str,
        playbook_display: str,
        inventory_id: int,
        inventory_name: str,
        host_count: int,
        extra_vars: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> MCPResult:
        """Execute a playbook and wait for completion with output."""
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

            # Step 4: Wait for job completion and get output
            final_status, output = await self._wait_for_job_completion(job_id)

            status_emoji = self._get_status_emoji(final_status)

            # Parse and clean up output for readability
            output = self._format_ansible_output(output)

            # Truncate output for Slack (max ~3000 chars)
            if len(output) > 2500:
                output = output[:2500] + "\n... (truncated, see AWX for full output)"

            # Create descriptive title
            playbook_short = playbook_display.replace('.yml', '').replace('.yaml', '').replace('/', '-')

            # Add error hints if failed
            error_hint = ""
            if final_status == "failed":
                if "Permission denied" in output or "UNREACHABLE" in output:
                    error_hint = (
                        "\n\n💡 *Hint:* SSH authentication failed. "
                        "Check that AWX has the correct SSH credential configured. "
                        "Admin needs to set `AWX_CREDENTIAL_ID` in secrets."
                    )
                elif "No such file" in output:
                    error_hint = "\n\n💡 *Hint:* Playbook file not found. Check the path."

            return MCPResult(
                status=MCPResultStatus.SUCCESS if final_status == "successful" else MCPResultStatus.ERROR,
                message=(
                    f"{status_emoji} *{playbook_short}* on `{inventory_name}`\n\n"
                    f"*Status:* `{final_status}` | *Job:* `{job_id}` | *Hosts:* {host_count}\n"
                    f"{error_hint}\n"
                    f"*Output:*\n```\n{output}\n```\n\n"
                    f"<{job_url}|View in AWX>\n\n"
                    f"_Ready for next task_"
                ),
                data={"job_id": job_id, "status": final_status, "output": output}
            )

        except Exception as e:
            logger.exception("Error executing playbook")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error executing playbook:* {str(e)}\n\n_Ready for next task_"
            )

    async def _wait_for_job_completion(self, job_id: int, timeout: int = 300) -> tuple:
        """Wait for job to complete and return (status, output)."""
        import time
        start_time = time.time()

        while time.time() - start_time < timeout:
            job = await self._get_job(job_id)
            if not job:
                return ("error", "Job not found")

            status = job.get("status", "unknown")

            # Check if job is done
            if status in ["successful", "failed", "error", "canceled"]:
                output = await self._get_job_output(job_id, lines=100)
                return (status, output or "No output available")

            # Wait before checking again
            await asyncio.sleep(3)

        # Timeout - get whatever output we have
        output = await self._get_job_output(job_id, lines=50)
        return ("timeout", output or "Job timed out waiting for completion")

    async def _execute_playbook(
        self,
        playbook: str,
        inventory_id: int,
        inventory_name: str,
        extra_vars: Optional[str] = None,
    ) -> MCPResult:
        """Execute a playbook on AWX (legacy, used by confirmed actions)."""
        return await self._execute_playbook_with_streaming(
            playbook=playbook,
            playbook_display=playbook,
            inventory_id=inventory_id,
            inventory_name=inventory_name,
            host_count=0,
            extra_vars=extra_vars,
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

    async def _handle_show_playbook(self, parameters: Dict[str, Any]) -> MCPResult:
        """Show details about a playbook (file or folder)."""
        playbook = parameters.get("playbook", "").strip()

        if not playbook:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    "⚠️ *Playbook name required*\n\n"
                    "Usage: `show playbook <name>`\n"
                    "Example: `show playbook check-service`\n\n"
                    "_Ready for next task_"
                )
            )

        # Remove extension if provided
        playbook_base = playbook.replace(".yml", "").replace(".yaml", "")

        try:
            # First check if it's a known playbook (file or folder)
            playbooks = await self._fetch_playbooks_from_github()

            # Find matching playbook
            matching_pb = None
            for pb in playbooks:
                name = pb.get("name", "")
                name_base = name.replace(".yml", "").replace(".yaml", "")
                if name_base.lower() == playbook_base.lower():
                    matching_pb = pb
                    break

            if not matching_pb:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=(
                        f"⚠️ *Playbook not found:* `{playbook}`\n\n"
                        "Use `list playbooks` to see available playbooks.\n\n"
                        "_Ready for next task_"
                    )
                )

            pb_type = matching_pb.get("type", "file")
            pb_name = matching_pb.get("name", "")

            if pb_type == "folder":
                # It's a folder - show contents
                files = await self._fetch_playbook_files(pb_name)

                if not files:
                    return MCPResult(
                        status=MCPResultStatus.SUCCESS,
                        message=(
                            f"📂 *Playbook Folder:* `{pb_name}/`\n\n"
                            "_(empty folder)_\n\n"
                            "_Ready for next task_"
                        )
                    )

                lines = [f"📂 *Playbook Folder:* `{pb_name}/`\n"]
                lines.append("*Contents:*")

                yml_files = []
                other_files = []
                for f in files:
                    name = f.get("name", "")
                    ftype = f.get("type", "file")
                    if name.endswith((".yml", ".yaml")):
                        yml_files.append(f"  • `{name}` _(playbook)_")
                    elif ftype == "dir":
                        other_files.append(f"  • `{name}/` _(folder)_")
                    else:
                        other_files.append(f"  • `{name}`")

                lines.extend(yml_files)
                if other_files:
                    lines.append("\n*Other files:*")
                    lines.extend(other_files)

                lines.append(f"\n*Total:* {len(yml_files)} playbook(s), {len(other_files)} other")
                lines.append(f"\n_To run: `run playbook {pb_name}/<file> on <inventory>`_")
                lines.append("\n_Ready for next task_")

                return MCPResult(
                    status=MCPResultStatus.SUCCESS,
                    message="\n".join(lines),
                    data={"playbook": pb_name, "type": "folder", "files": files}
                )
            else:
                # It's a standalone file
                return MCPResult(
                    status=MCPResultStatus.SUCCESS,
                    message=(
                        f"📄 *Playbook File:* `{pb_name}`\n\n"
                        f"This is a standalone playbook file.\n\n"
                        f"_To run: `run playbook {playbook_base} on <inventory>`_\n\n"
                        f"_Ready for next task_"
                    ),
                    data={"playbook": pb_name, "type": "file"}
                )

        except Exception as e:
            logger.exception("Error showing playbook")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error:* {str(e)}\n\n_Ready for next task_"
            )

    async def _handle_queue_status(self) -> MCPResult:
        """Get current queue status."""
        if not self._queue:
            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=(
                    "📊 *Queue Status*\n\n"
                    "Queue is not enabled. Running playbooks directly.\n\n"
                    "_Ready for next task_"
                )
            )

        try:
            status_msg = await self._queue.get_status()
            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=f"{status_msg}\n\n_Ready for next task_"
            )
        except Exception as e:
            logger.exception("Error getting queue status")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error getting queue status:* {str(e)}\n\n_Ready for next task_"
            )

    async def _handle_my_requests(self, user_id: str) -> MCPResult:
        """Get user's recent requests."""
        if not self._queue:
            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=(
                    "📋 *Your Requests*\n\n"
                    "Queue is not enabled. No request tracking available.\n\n"
                    "_Ready for next task_"
                )
            )

        try:
            requests_msg = await self._queue.get_user_requests(user_id)
            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=f"{requests_msg}\n\n_Ready for next task_"
            )
        except Exception as e:
            logger.exception("Error getting user requests")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error getting your requests:* {str(e)}\n\n_Ready for next task_"
            )

    async def _handle_cancel_request(self, parameters: Dict[str, Any], user_id: str) -> MCPResult:
        """Cancel a pending request."""
        request_id = parameters.get("request_id", "").strip()

        if not request_id:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    "⚠️ *Request ID required*\n\n"
                    "Usage: `cancel request <request_id>`\n"
                    "Use `my requests` to see your pending requests.\n\n"
                    "_Ready for next task_"
                )
            )

        if not self._queue:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Queue is not enabled.\n\n_Ready for next task_"
            )

        try:
            success, message = await self._queue.cancel(request_id, user_id)
            return MCPResult(
                status=MCPResultStatus.SUCCESS if success else MCPResultStatus.ERROR,
                message=f"{message}\n\n_Ready for next task_"
            )
        except Exception as e:
            logger.exception("Error canceling request")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error canceling request:* {str(e)}\n\n_Ready for next task_"
            )

    async def _execute_playbook_internal(
        self,
        playbook: str,
        inventory: str,
        extra_vars: Optional[Dict[str, Any]] = None,
    ) -> MCPResult:
        """Execute playbook internally (used by queue executor)."""
        # Get inventory info
        inventory_info = await self._get_inventory(inventory)
        if not inventory_info:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ Unknown inventory: `{inventory}`"
            )

        inv_id = inventory_info.get("id")
        host_count = inventory_info.get("total_hosts", 0)

        # Convert extra_vars dict to JSON string if needed
        extra_vars_str = None
        if extra_vars:
            import json
            extra_vars_str = json.dumps(extra_vars)

        return await self._execute_playbook_with_streaming(
            playbook=playbook,
            playbook_display=playbook,
            inventory_id=inv_id,
            inventory_name=inventory,
            host_count=host_count,
            extra_vars=extra_vars_str,
        )

    async def _handle_setup_ssh(self) -> MCPResult:
        """Check and display SSH credential status."""
        try:
            # Check if AWX_CREDENTIAL_ID is configured
            if self.awx_credential_id:
                # Verify the credential exists in AWX
                cred_info = await self._get_credential(int(self.awx_credential_id))
                if cred_info:
                    return MCPResult(
                        status=MCPResultStatus.SUCCESS,
                        message=(
                            f"✅ *SSH Credential Configured*\n\n"
                            f"*Credential ID:* `{self.awx_credential_id}`\n"
                            f"*Name:* `{cred_info.get('name', 'unknown')}`\n"
                            f"*Type:* `{cred_info.get('credential_type_name', 'Machine')}`\n\n"
                            f"SSH is ready for playbook execution.\n\n"
                            f"_Ready for next task_"
                        )
                    )
                else:
                    return MCPResult(
                        status=MCPResultStatus.ERROR,
                        message=(
                            f"⚠️ *SSH Credential Not Found*\n\n"
                            f"Configured ID `{self.awx_credential_id}` does not exist in AWX.\n\n"
                            f"*To fix:*\n"
                            f"1. Go to AWX: http://{self.awx_server}/#/credentials\n"
                            f"2. Create a 'Machine' credential with SSH key\n"
                            f"3. Update `AWX_CREDENTIAL_ID` in K8s secrets\n\n"
                            f"_Ready for next task_"
                        )
                    )
            else:
                # List existing SSH credentials
                creds = await self._list_ssh_credentials()

                if creds:
                    cred_list = "\n".join([f"  • ID `{c['id']}`: {c['name']}" for c in creds])
                    return MCPResult(
                        status=MCPResultStatus.ERROR,
                        message=(
                            f"⚠️ *SSH Credential Not Configured*\n\n"
                            f"AWX has these SSH credentials:\n{cred_list}\n\n"
                            f"*To fix:*\n"
                            f"Add to K8s secrets:\n"
                            f"```\nkubectl patch secret slack-mcp-agent-secrets -p "
                            f"'{{\"stringData\":{{\"AWX_CREDENTIAL_ID\":\"<id>\"}}}}'\n```\n"
                            f"Then restart the pod.\n\n"
                            f"_Ready for next task_"
                        )
                    )
                else:
                    return MCPResult(
                        status=MCPResultStatus.ERROR,
                        message=(
                            f"⚠️ *No SSH Credentials in AWX*\n\n"
                            f"*To fix:*\n"
                            f"1. Go to AWX: http://{self.awx_server}/#/credentials\n"
                            f"2. Click 'Add'\n"
                            f"3. Select 'Machine' credential type\n"
                            f"4. Enter name: `vivox-ssh-key`\n"
                            f"5. Paste your SSH private key\n"
                            f"6. Save and note the ID\n"
                            f"7. Add to K8s secrets:\n"
                            f"```\nkubectl patch secret slack-mcp-agent-secrets -p "
                            f"'{{\"stringData\":{{\"AWX_CREDENTIAL_ID\":\"<id>\"}}}}'\n```\n\n"
                            f"_Ready for next task_"
                        )
                    )

        except Exception as e:
            logger.exception("Error checking SSH setup")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ *Error:* {str(e)}\n\n_Ready for next task_"
            )

    async def _get_credential(self, cred_id: int) -> Optional[Dict[str, Any]]:
        """Get credential details from AWX."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._get_credential_sync,
                cred_id,
            )
        except Exception as e:
            logger.error(f"Error getting credential: {e}")
            return None

    def _get_credential_sync(self, cred_id: int) -> Optional[Dict[str, Any]]:
        """Synchronously get credential from AWX."""
        url = f"http://{self.awx_server}/api/v2/credentials/{cred_id}/"
        response = requests.get(
            url,
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def _list_ssh_credentials(self) -> List[Dict[str, Any]]:
        """List SSH (Machine) credentials from AWX."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._list_ssh_credentials_sync,
            )
        except Exception as e:
            logger.error(f"Error listing credentials: {e}")
            return []

    def _list_ssh_credentials_sync(self) -> List[Dict[str, Any]]:
        """Synchronously list SSH credentials from AWX."""
        # First get Machine credential type ID
        type_url = f"http://{self.awx_server}/api/v2/credential_types/"
        type_response = requests.get(
            type_url,
            params={"name": "Machine"},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        type_response.raise_for_status()
        types = type_response.json().get("results", [])
        if not types:
            return []
        machine_type_id = types[0]["id"]

        # Get credentials of that type
        url = f"http://{self.awx_server}/api/v2/credentials/"
        response = requests.get(
            url,
            params={"credential_type": machine_type_id},
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()
        return response.json().get("results", [])

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

    def _format_ansible_output(self, output: str) -> str:
        """Parse and format Ansible output for better Slack readability."""
        import re

        # Remove ANSI color codes if any
        output = re.sub(r'\x1b\[[0-9;]*m', '', output)

        # Fix escaped newlines and quotes
        output = output.replace('\\n', '\n').replace('\\"', '"')

        # Extract key information
        lines = output.split('\n')
        formatted_lines = []
        in_task = False
        current_task = ""
        services_found = []
        hosts_summary = {}

        for line in lines:
            line = line.strip()

            # Skip deprecation warnings and empty lines
            if not line or '[DEPRECATION WARNING]' in line or line.startswith('deprecation_warnings'):
                continue
            if 'keyword is a more generic' in line or 'This feature will be removed' in line:
                continue
            if '[WARNING]: Invalid characters' in line or 'use -vvvv to see details' in line:
                continue
            if line.startswith('Identity added:'):
                continue

            # Capture PLAY name
            if line.startswith('PLAY ['):
                play_name = re.search(r'PLAY \[(.+?)\]', line)
                if play_name:
                    formatted_lines.append(f"▶️ {play_name.group(1)}")
                continue

            # Capture TASK name
            if line.startswith('TASK ['):
                task_name = re.search(r'TASK \[(.+?)\]', line)
                if task_name:
                    current_task = task_name.group(1)
                    in_task = True
                continue

            # Capture task results
            if line.startswith('ok:') or line.startswith('changed:'):
                host = re.search(r'\[(.+?)\]', line)
                if host:
                    host_name = host.group(1)
                    if host_name not in hosts_summary:
                        hosts_summary[host_name] = {'ok': 0, 'changed': 0, 'failed': 0}
                    if line.startswith('ok:'):
                        hosts_summary[host_name]['ok'] += 1
                    else:
                        hosts_summary[host_name]['changed'] += 1
                continue

            if line.startswith('fatal:') or line.startswith('failed:'):
                host = re.search(r'\[(.+?)\]', line)
                if host:
                    host_name = host.group(1)
                    if host_name not in hosts_summary:
                        hosts_summary[host_name] = {'ok': 0, 'changed': 0, 'failed': 0}
                    hosts_summary[host_name]['failed'] += 1
                    # Include error message
                    formatted_lines.append(f"❌ {host_name}: {current_task}")
                    # Try to extract error message
                    error_match = re.search(r'"msg":\s*"(.+?)"', line)
                    if error_match:
                        formatted_lines.append(f"   Error: {error_match.group(1)[:200]}")
                continue

            if line.startswith('skipping:'):
                continue

            # Capture service status from our playbook output
            if '"msg":' in line:
                # Try to extract service information
                msg_match = re.search(r'"msg":\s*"(.+)"', line)
                if msg_match:
                    msg = msg_match.group(1)
                    # Look for service status patterns
                    service_matches = re.findall(r'(\w+):\s*(active|inactive|failed|dead)', msg.lower())
                    for svc, status in service_matches:
                        emoji = "🟢" if status == "active" else "🔴"
                        services_found.append(f"{emoji} {svc}: {status}")

            # Capture PLAY RECAP
            if line.startswith('PLAY RECAP'):
                formatted_lines.append("\n📊 Summary:")
                continue

            # Parse recap lines
            recap_match = re.match(r'^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)', line)
            if recap_match:
                host, ok, changed, unreachable, failed = recap_match.groups()
                status = "✅" if int(failed) == 0 and int(unreachable) == 0 else "❌"
                formatted_lines.append(f"  {status} {host}: ok={ok}, changed={changed}, failed={failed}")

        # Build final output
        result = []

        if formatted_lines:
            result.extend(formatted_lines)

        if services_found:
            result.append("\n🔧 Services Detected:")
            result.extend([f"  {s}" for s in services_found])

        return '\n'.join(result) if result else output[:1000]

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
            item_type = item.get("type", "")

            # Include directories (playbook folders) and yml files
            if item_type == "dir":
                # It's a playbook folder (like check-service/, harjo-setup/)
                playbooks.append({
                    "name": name,
                    "path": item.get("path"),
                    "sha": item.get("sha"),
                    "url": item.get("html_url"),
                    "type": "folder",
                })
            elif name.endswith((".yml", ".yaml")) and item_type == "file":
                # It's a standalone playbook file
                playbooks.append({
                    "name": name,
                    "path": item.get("path"),
                    "sha": item.get("sha"),
                    "url": item.get("html_url"),
                    "type": "file",
                })

        return playbooks

    async def _fetch_playbook_files(self, playbook_name: str) -> List[Dict[str, Any]]:
        """Fetch files inside a playbook folder."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._fetch_playbook_files_sync,
                playbook_name,
            )
        except Exception as e:
            logger.error(f"Error fetching playbook files: {e}")
            return []

    def _fetch_playbook_files_sync(self, playbook_name: str) -> List[Dict[str, Any]]:
        """Synchronously fetch files from a playbook folder."""
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        # Get contents of the playbook folder
        folder_path = f"{self.github_path}/{playbook_name}"
        url = f"{self.github_api_url}/repos/{self.github_repo}/contents/{folder_path}"
        params = {"ref": self.github_branch}

        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 404:
            return []

        response.raise_for_status()

        contents = response.json()
        files = []

        for item in contents:
            files.append({
                "name": item.get("name", ""),
                "path": item.get("path"),
                "type": item.get("type", "file"),
                "sha": item.get("sha"),
                "url": item.get("html_url"),
            })

        return files

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
        # Template name based on playbook - make it descriptive
        playbook_base = playbook.replace('.yml', '').replace('.yaml', '').replace('/', '-')
        template_name = f"slack-bot-{playbook_base}"

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
            template_id = template["id"]

            # Update inventory if different
            if template.get("inventory") != inventory_id:
                update_url = f"http://{self.awx_server}/api/v2/job_templates/{template_id}/"
                requests.patch(
                    update_url,
                    json={"inventory": inventory_id},
                    auth=(self.awx_username, self.awx_password),
                    timeout=30
                )

            # Ensure credential is attached (AWX uses separate endpoint)
            if self.awx_credential_id:
                existing_creds = template.get("summary_fields", {}).get("credentials", [])
                cred_ids = [c.get("id") for c in existing_creds]
                cred_id = int(self.awx_credential_id)

                if cred_id not in cred_ids:
                    cred_url = f"http://{self.awx_server}/api/v2/job_templates/{template_id}/credentials/"
                    try:
                        requests.post(
                            cred_url,
                            json={"id": cred_id},
                            auth=(self.awx_username, self.awx_password),
                            timeout=30
                        )
                        logger.info(f"Attached credential {cred_id} to existing template {template_id}")
                    except Exception as e:
                        logger.warning(f"Failed to attach credential: {e}")

            return template_id

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

        response = requests.post(
            url,
            json=payload,
            auth=(self.awx_username, self.awx_password),
            timeout=30
        )
        response.raise_for_status()

        template_id = response.json().get("id")

        # Attach credential via separate endpoint (AWX requires this)
        if self.awx_credential_id and template_id:
            cred_url = f"http://{self.awx_server}/api/v2/job_templates/{template_id}/credentials/"
            try:
                requests.post(
                    cred_url,
                    json={"id": int(self.awx_credential_id)},
                    auth=(self.awx_username, self.awx_password),
                    timeout=30
                )
                logger.info(f"Attached credential {self.awx_credential_id} to template {template_id}")
            except Exception as e:
                logger.warning(f"Failed to attach credential: {e}")

        return template_id

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
