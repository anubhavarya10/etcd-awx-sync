"""MCP implementation for AWX playbook operations."""

import os
import asyncio
import time
import logging
import requests
import urllib3
from typing import Any, Dict, List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor

# Disable SSL warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

    # Pre-configured repo presets (name -> config)
    REPO_PRESETS = {
        "vivox-ops-docker": {
            "api_url": "https://github.cds.internal.unity3d.com/api/v3",
            "repo": "unity/vivox-ops-docker",
            "path": "ansible",
            "branch": "main",
            "token_env": "GITHUB_TOKEN",
            "scm_credential_name": "slack-bot-github-token",
        },
        "vivox-ops-ansible": {
            "api_url": "https://api.github.com",
            "repo": "Unity-Technologies/vivox-ops-ansible",
            "path": "general_playbooks",
            "branch": "main",
            "token_env": "TF_GITHUB_TOKEN",
            "scm_credential_name": "slack-bot-github-com-token",
        },
    }

    def __init__(self, notify_callback: Optional[Callable] = None):
        # AWX configuration
        self.awx_server = os.environ.get("AWX_SERVER", "localhost")
        self.awx_username = os.environ.get("AWX_USERNAME")
        self.awx_password = os.environ.get("AWX_PASSWORD")

        # GitHub configuration (defaults to vivox-ops-docker)
        self.github_api_url = os.environ.get(
            "GITHUB_API_URL",
            "https://github.cds.internal.unity3d.com/api/v3"
        )
        self.github_token = os.environ.get("GITHUB_TOKEN")
        self.github_repo = os.environ.get("GITHUB_PLAYBOOKS_REPO", "unity/vivox-ops-docker")
        self.github_path = os.environ.get("GITHUB_PLAYBOOKS_PATH", "ansible")
        self.github_branch = os.environ.get("GITHUB_PLAYBOOKS_BRANCH", "main")
        self._active_preset: Optional[str] = "vivox-ops-docker"
        self._scm_credential_name = "slack-bot-github-token"

        # AWX credential ID for SSH access to hosts
        self.awx_credential_id = os.environ.get("AWX_CREDENTIAL_ID")

        # AWX Execution Environment ID (e.g., EE4 with all requirements)
        self.awx_execution_environment_id = os.environ.get("AWX_EXECUTION_ENVIRONMENT_ID")

        # Azure SSH configuration (for hosts with IPs starting with 10.253.x.x)
        self.azure_sudo_password = os.environ.get("AZURE_SUDO_PASSWORD")

        # Cache for playbooks
        self._playbook_cache: Dict[str, Any] = {
            "playbooks": [],
            "last_refresh": 0,
        }
        self._cache_ttl = 300  # 5 minutes

        # Track AWX project ID (created once)
        self._awx_project_id: Optional[int] = None

        # Global runbook configuration
        self.global_inventory_name = os.environ.get("AWX_GLOBAL_INVENTORY", "central inventory")
        self.global_job_timeout = int(os.environ.get("AWX_GLOBAL_JOB_TIMEOUT", "3600"))  # 60 min
        self.global_progress_interval = int(os.environ.get("AWX_GLOBAL_PROGRESS_INTERVAL", "30"))  # 30s

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
        async def queue_executor(playbook, inventory, extra_vars, user_id, channel_id, message_ts=None):
            return await self._execute_playbook_internal(
                playbook=playbook,
                inventory=inventory,
                extra_vars=extra_vars,
                channel_id=channel_id,
                message_ts=message_ts,
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
            if parameters.get("global_mode"):
                return await self._handle_run_playbook_global(parameters, user_id, channel_id)
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
            # Not found in current repo — try other presets automatically
            for other_name, other_cfg in self.REPO_PRESETS.items():
                if other_name == self._active_preset:
                    continue
                # Temporarily fetch playbooks from the other repo
                other_token = os.environ.get(other_cfg["token_env"], "")
                if not other_token:
                    continue
                try:
                    headers = {"Accept": "application/vnd.github.v3+json"}
                    headers["Authorization"] = f"token {other_token}"
                    url = f"{other_cfg['api_url']}/repos/{other_cfg['repo']}/contents/{other_cfg['path']}"
                    loop = asyncio.get_event_loop()
                    resp = await loop.run_in_executor(
                        _executor,
                        lambda: requests.get(url, headers=headers, params={"ref": other_cfg["branch"]}, timeout=30).json()
                    )
                    if isinstance(resp, list):
                        other_names = [item.get("name", "").lower() for item in resp]
                        playbook_lower = playbook.lower()
                        if playbook_lower in other_names or playbook_lower + ".yml" in other_names or playbook_lower + ".yaml" in other_names:
                            # Found it in another preset — switch and retry
                            logger.info(f"Playbook '{playbook}' found in preset '{other_name}', auto-switching")
                            switch_result = await self._switch_to_preset(other_name)
                            if switch_result.status == MCPResultStatus.SUCCESS:
                                # Retry the run with the switched repo
                                return await self._handle_run_playbook(parameters, user_id, channel_id)
                except Exception as e:
                    logger.warning(f"Error checking preset {other_name} for playbook: {e}")

            # Still not found in any repo
            suggestions = [p.get("name", "") for p in playbooks[:5]]
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"⚠️ *Unknown playbook:* `{playbook}`\n\n"
                    f"*Available in current repo ({self._active_preset or self.github_repo}):* {', '.join(f'`{s}`' for s in suggestions)}\n\n"
                    "Use `list playbooks` to see all.\n"
                    "Use `set repo <preset>` to switch repos.\n\n"
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

            # Get message_ts from parameters for threading
            message_ts = parameters.get("_message_ts")

            request = PlaybookRequest.create(
                user_id=user_id,
                user_name=user_id,  # Will be user ID, Slack can resolve to name later
                channel_id=channel_id,
                playbook=playbook_path,
                inventory=inventory,
                extra_vars=extra_vars_dict,
                priority=RequestPriority.NORMAL,
                message_ts=message_ts,
            )

            success, message = await self._queue.submit(request)

            # Return threaded response for queue submission
            thread_msg = (
                f"🚀 *Request Submitted*\n\n"
                f"• Request ID: `{request.id}`\n"
                f"• Playbook: `{playbook_path}`\n"
                f"• Inventory: `{inventory}`\n"
                f"• Priority: `{RequestPriority.NORMAL.name}`\n\n"
                f"⏳ {'Starting immediately...' if success else 'Queued...'}"
            )

            return MCPResult(
                status=MCPResultStatus.SUCCESS if success else MCPResultStatus.ERROR,
                message="⏳ Request submitted..." if success else message,
                data={"request_id": request.id if success else None},
                thread_messages=[thread_msg] if success else [message],
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
            # Step 1: Ensure AWX project exists and is synced
            project_id = await self._ensure_awx_project()
            if not project_id:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ Failed to create AWX project",
                    thread_messages=["❌ *Error:* Failed to create AWX project. Check AWX credentials."],
                )

            # Sync the project and wait for completion so playbooks are available
            await self._sync_awx_project(project_id)
            await self._wait_for_project_sync(project_id)

            # Step 2: Create or get job template
            template_id = await self._ensure_job_template(
                playbook=playbook,
                project_id=project_id,
                inventory_id=inventory_id,
            )
            if not template_id:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ Failed to create job template",
                    thread_messages=["❌ *Error:* Failed to create job template. Check playbook path."],
                )

            # Step 3: Check if inventory has Azure hosts (10.253.x.x)
            # Azure hosts require vivoxops user with sudo instead of root
            is_azure = self._is_azure_inventory(inventory_id)
            if is_azure and self.azure_sudo_password:
                logger.info(f"Azure inventory detected, using vivoxops user with sudo")
                azure_vars = self._get_azure_extra_vars()
                # Merge with existing extra_vars if any
                if extra_vars:
                    try:
                        import json
                        existing_vars = json.loads(extra_vars) if isinstance(extra_vars, str) else extra_vars
                        existing_vars.update(azure_vars)
                        extra_vars = json.dumps(existing_vars)
                    except (json.JSONDecodeError, TypeError):
                        extra_vars = json.dumps(azure_vars)
                else:
                    import json
                    extra_vars = json.dumps(azure_vars)

            # Step 4: Launch the job
            job = await self._launch_job(template_id, extra_vars)
            if not job:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ Failed to launch job",
                    thread_messages=["❌ *Error:* Failed to launch AWX job. Check AWX configuration."],
                )

            job_id = job.get("id")
            job_url = f"https://{self.awx_server}/#/jobs/playbook/{job_id}"

            # Step 4: Wait for job completion and get output
            final_status, raw_output = await self._wait_for_job_completion(job_id)

            status_emoji = self._get_status_emoji(final_status)
            result_text = "passed" if final_status == "successful" else "failed"

            # Parse and clean up output for readability
            formatted_output = self._format_ansible_output(raw_output)

            # Truncate output for Slack (max ~3000 chars)
            if len(formatted_output) > 2500:
                formatted_output = formatted_output[:2500] + "\n... (truncated, see AWX for full output)"

            # Create descriptive title
            playbook_short = playbook_display.replace('.yml', '').replace('.yaml', '').replace('/', '-')

            # Build thread messages with detailed info
            thread_messages = []

            # Add job details
            thread_messages.append(
                f"📋 *Job Details*\n"
                f"• Playbook: `{playbook_display}`\n"
                f"• Inventory: `{inventory_name}`\n"
                f"• Job ID: `{job_id}`\n"
                f"• Hosts: {host_count}"
            )

            # Add diagnosis if failed
            if final_status == "failed":
                diagnosis, solution = self._diagnose_failure(formatted_output, raw_output)
                thread_messages.append(
                    f"🔴 *Diagnosis:* {diagnosis}\n\n"
                    f"💡 *Solution:*\n{solution}"
                )

            # Add output
            thread_messages.append(
                f"📄 *Output:*\n```\n{formatted_output}\n```"
            )

            # Main message update (simple result line)
            main_update = f"*Result:* {status_emoji} {result_text} | Job: `{job_id}`"

            return MCPResult(
                status=MCPResultStatus.SUCCESS if final_status == "successful" else MCPResultStatus.ERROR,
                message=main_update,
                data={"job_id": job_id, "status": final_status, "output": formatted_output},
                thread_messages=thread_messages,
                main_message_update=main_update,
                awx_url=job_url,
            )

        except Exception as e:
            logger.exception("Error executing playbook")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ Error: {str(e)}",
                thread_messages=[f"❌ *Error executing playbook:*\n```\n{str(e)}\n```"],
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
                # If no output (e.g., job never started due to project sync failure),
                # include the job_explanation which contains the actual error
                if not output or output.strip() == "":
                    job_explanation = job.get("job_explanation", "")
                    if job_explanation:
                        output = f"Job did not run: {job_explanation}"
                    else:
                        output = "No output available"
                return (status, output)

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

            job_url = f"https://{self.awx_server}/#/jobs/playbook/{job_id}"

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

            job_url = f"https://{self.awx_server}/#/jobs/playbook/{job_id}"

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
        """Set the GitHub repository for playbooks. Supports preset names or custom repos."""
        repo = parameters.get("repo", "").strip()
        path = parameters.get("path", "").strip()
        branch = parameters.get("branch", "").strip()

        if not repo:
            # Show available presets
            preset_lines = []
            for name, cfg in self.REPO_PRESETS.items():
                active = " *(active)*" if name == self._active_preset else ""
                preset_lines.append(f"  `{name}` - `{cfg['repo']}` (path: `{cfg['path']}`){active}")

            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    ":warning: *Repository required*\n\n"
                    "*Available presets:*\n" + "\n".join(preset_lines) + "\n\n"
                    "Usage:\n"
                    "  `set repo vivox-ops-ansible` — switch to a preset\n"
                    "  `set repo <org/repo-name> [path <folder>] [branch <branch>]` — custom repo\n\n"
                    "_Ready for next task_"
                )
            )

        # Check if it's a preset name
        if repo in self.REPO_PRESETS:
            return await self._switch_to_preset(repo)

        # Also match partial preset names
        for preset_name in self.REPO_PRESETS:
            if repo.lower() in preset_name.lower():
                return await self._switch_to_preset(preset_name)

        # Validate repo format (should be org/repo)
        if "/" not in repo:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f":warning: *Invalid repository format:* `{repo}`\n\n"
                    "Expected format: `org/repo-name` or a preset name\n"
                    "Example: `unity/vivox-ops-docker` or `vivox-ops-ansible`\n\n"
                    "_Ready for next task_"
                )
            )

        old_repo = self.github_repo
        old_path = self.github_path
        old_branch = self.github_branch
        old_preset = self._active_preset

        # Update configuration
        self.github_repo = repo
        self._active_preset = None
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
                self._active_preset = old_preset
                self._playbook_cache = {"playbooks": [], "last_refresh": 0}

                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=(
                        f":warning: *No playbooks found in new repo*\n\n"
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
                    f":white_check_mark: *Repository Updated*\n\n"
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
            self._active_preset = old_preset
            self._playbook_cache = {"playbooks": [], "last_refresh": 0}

            logger.exception("Error setting repo")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f":x: *Error accessing repository:* {str(e)}\n\n"
                    "Configuration not changed.\n\n"
                    "_Ready for next task_"
                )
            )

    async def _switch_to_preset(self, preset_name: str) -> MCPResult:
        """Switch to a pre-configured repo preset."""
        preset = self.REPO_PRESETS[preset_name]

        old_api_url = self.github_api_url
        old_token = self.github_token
        old_repo = self.github_repo
        old_path = self.github_path
        old_branch = self.github_branch
        old_preset = self._active_preset
        old_scm_name = self._scm_credential_name

        # Apply preset
        self.github_api_url = preset["api_url"]
        self.github_token = os.environ.get(preset["token_env"], "")
        self.github_repo = preset["repo"]
        self.github_path = preset["path"]
        self.github_branch = preset["branch"]
        self._active_preset = preset_name
        self._scm_credential_name = preset["scm_credential_name"]

        # Clear cache and reset project
        self._playbook_cache = {"playbooks": [], "last_refresh": 0}
        self._awx_project_id = None

        # Validate by fetching playbooks
        try:
            playbooks = await self._fetch_playbooks_from_github()

            if not playbooks:
                # Revert
                self.github_api_url = old_api_url
                self.github_token = old_token
                self.github_repo = old_repo
                self.github_path = old_path
                self.github_branch = old_branch
                self._active_preset = old_preset
                self._scm_credential_name = old_scm_name
                self._playbook_cache = {"playbooks": [], "last_refresh": 0}

                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=(
                        f":warning: *No playbooks found in `{preset_name}`*\n\n"
                        f"Repository: `{preset['repo']}`\n"
                        f"Path: `{preset['path']}`\n\n"
                        "Configuration not changed.\n\n"
                        "_Ready for next task_"
                    )
                )

            # Build playbook listing
            playbook_lines = []
            folders = [p for p in playbooks if p.get("type") == "folder"]
            files = [p for p in playbooks if p.get("type") == "file"]

            if folders:
                playbook_lines.append("*Playbook Folders:*")
                for p in sorted(folders, key=lambda x: x["name"]):
                    playbook_lines.append(f"  :file_folder: `{p['name']}/`")

            if files:
                playbook_lines.append("*Playbook Files:*")
                for p in sorted(files, key=lambda x: x["name"]):
                    playbook_lines.append(f"  :page_facing_up: `{p['name']}`")

            playbook_list = "\n".join(playbook_lines) if playbook_lines else ""

            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=(
                    f":white_check_mark: *Switched to `{preset_name}`*\n\n"
                    f"*Repository:* `{self.github_repo}`\n"
                    f"*Path:* `{self.github_path}`\n"
                    f"*Branch:* `{self.github_branch}`\n\n"
                    f"*Playbooks available:* {len(playbooks)}\n\n"
                    f"{playbook_list}\n\n"
                    "To run: `/awx run <playbook> on <inventory>`\n\n"
                    "_Ready for next task_"
                ),
                data={"preset": preset_name, "repo": self.github_repo, "path": self.github_path}
            )

        except Exception as e:
            # Revert
            self.github_api_url = old_api_url
            self.github_token = old_token
            self.github_repo = old_repo
            self.github_path = old_path
            self.github_branch = old_branch
            self._active_preset = old_preset
            self._scm_credential_name = old_scm_name
            self._playbook_cache = {"playbooks": [], "last_refresh": 0}

            logger.exception(f"Error switching to preset {preset_name}")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f":x: *Error accessing `{preset_name}`:* {str(e)}\n\n"
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

        # Build preset list
        preset_lines = []
        for name, cfg in self.REPO_PRESETS.items():
            if name == self._active_preset:
                preset_lines.append(f"  :white_check_mark: `{name}` - `{cfg['repo']}` (path: `{cfg['path']}`) *(active)*")
            else:
                preset_lines.append(f"  :white_circle: `{name}` - `{cfg['repo']}` (path: `{cfg['path']}`)")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=(
                f":file_folder: *Playbook Repository Configuration*\n\n"
                f"*Repository:* `{self.github_repo}`\n"
                f"*Path:* `{self.github_path}`\n"
                f"*Branch:* `{self.github_branch}`\n"
                f"*API URL:* `{self.github_api_url}`\n\n"
                f"*Playbooks available:* {playbook_count}\n\n"
                f"*Available presets:*\n" + "\n".join(preset_lines) + "\n\n"
                f"To switch: `set repo <preset-name>` (e.g. `set repo vivox-ops-ansible`)\n"
                f"Custom: `set repo <org/repo> [path <folder>] [branch <branch>]`\n\n"
                f"_Ready for next task_"
            ),
            data={
                "repo": self.github_repo,
                "path": self.github_path,
                "branch": self.github_branch,
                "api_url": self.github_api_url,
                "playbook_count": playbook_count,
                "active_preset": self._active_preset,
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
        channel_id: Optional[str] = None,
        message_ts: Optional[str] = None,
    ) -> MCPResult:
        """Execute playbook internally (used by queue executor)."""
        # Check for global mode flag in extra_vars
        is_global = extra_vars and extra_vars.pop("_global_mode", False)

        # Get inventory info
        inventory_info = await self._get_inventory(inventory)
        if not inventory_info:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ Unknown inventory: `{inventory}`"
            )

        inv_id = inventory_info.get("id")
        host_count = inventory_info.get("total_hosts", 0)

        if is_global:
            return await self._execute_playbook_global(
                playbook=playbook,
                inventory_id=inv_id,
                inventory_name=inventory,
                host_count=host_count,
                extra_vars=extra_vars,
                channel_id=channel_id,
                message_ts=message_ts,
            )

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

    async def _handle_run_playbook_global(
        self,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """Handle running a playbook globally across all hosts (central inventory)."""
        playbook = parameters.get("playbook", "")
        extra_vars = parameters.get("extra_vars")

        # Resolve playbook path (reuse existing logic from _handle_run_playbook)
        playbook_path = playbook
        playbooks = await self._fetch_playbooks_from_github()

        folder_match = None
        file_match = None
        for pb in playbooks:
            name = pb.get("name", "")
            pb_type = pb.get("type", "file")
            if pb_type == "folder":
                if playbook.lower().rstrip("/") == name.lower():
                    folder_match = name
                    break
            else:
                name_lower = name.lower()
                playbook_lower = playbook.lower()
                if playbook_lower == name_lower or playbook_lower + ".yml" == name_lower:
                    file_match = name
                    break

        if folder_match:
            files = await self._fetch_playbook_files(folder_match)
            yml_files = [f for f in files if f.get("name", "").endswith((".yml", ".yaml"))]
            if not yml_files:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=f"⚠️ *No playbooks found in folder:* `{folder_match}/`\n\n_Ready for next task_"
                )
            if len(yml_files) == 1:
                playbook_path = f"{folder_match}/{yml_files[0]['name']}"
            else:
                main_playbook = None
                yml_names = [f.get("name", "") for f in yml_files]
                for name in yml_names:
                    if name.replace(".yml", "").replace(".yaml", "") == folder_match:
                        main_playbook = name
                        break
                if not main_playbook:
                    for common_name in ["main.yml", "site.yml", "playbook.yml", "setup.yml"]:
                        if common_name in yml_names:
                            main_playbook = common_name
                            break
                if main_playbook:
                    playbook_path = f"{folder_match}/{main_playbook}"
                else:
                    file_list = "\n".join([f"  • `{folder_match}/{f['name']}`" for f in yml_files])
                    return MCPResult(
                        status=MCPResultStatus.ERROR,
                        message=(
                            f"📂 *Multiple playbooks in folder:* `{folder_match}/`\n\n"
                            f"{file_list}\n\n"
                            f"Please specify which one:\n"
                            f"`run <folder>/<filename>.yml globally`\n\n"
                            "_Ready for next task_"
                        )
                    )
        elif file_match:
            playbook_path = file_match
        elif "/" in playbook:
            playbook_path = playbook
            if not playbook_path.endswith((".yml", ".yaml")):
                playbook_path = f"{playbook_path}.yml"
        else:
            # Try other presets (same as _handle_run_playbook)
            for other_name, other_cfg in self.REPO_PRESETS.items():
                if other_name == self._active_preset:
                    continue
                other_token = os.environ.get(other_cfg["token_env"], "")
                if not other_token:
                    continue
                try:
                    headers = {"Accept": "application/vnd.github.v3+json"}
                    headers["Authorization"] = f"token {other_token}"
                    url = f"{other_cfg['api_url']}/repos/{other_cfg['repo']}/contents/{other_cfg['path']}"
                    loop = asyncio.get_event_loop()
                    resp = await loop.run_in_executor(
                        _executor,
                        lambda: requests.get(url, headers=headers, params={"ref": other_cfg["branch"]}, timeout=30).json()
                    )
                    if isinstance(resp, list):
                        other_names = [item.get("name", "").lower() for item in resp]
                        playbook_lower = playbook.lower()
                        if playbook_lower in other_names or playbook_lower + ".yml" in other_names:
                            switch_result = await self._switch_to_preset(other_name)
                            if switch_result.status == MCPResultStatus.SUCCESS:
                                return await self._handle_run_playbook_global(parameters, user_id, channel_id)
                except Exception as e:
                    logger.warning(f"Error checking preset {other_name}: {e}")

            suggestions = [p.get("name", "") for p in playbooks[:5]]
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"⚠️ *Unknown playbook:* `{playbook}`\n\n"
                    f"*Available:* {', '.join(f'`{s}`' for s in suggestions)}\n\n"
                    "Use `list playbooks` to see all.\n\n"
                    "_Ready for next task_"
                )
            )

        # Look up central inventory
        inventory_name = self.global_inventory_name
        inventory_info = await self._get_inventory(inventory_name)
        if not inventory_info:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"⚠️ *Global inventory not found:* `{inventory_name}`\n\n"
                    "The central inventory must exist in AWX.\n"
                    "Set `AWX_GLOBAL_INVENTORY` env var if using a different name.\n\n"
                    "_Ready for next task_"
                )
            )

        # Submit to queue with _global_mode flag
        if self._queue_enabled and self._queue:
            extra_vars_dict = {}
            if extra_vars:
                try:
                    import json
                    extra_vars_dict = json.loads(extra_vars) if isinstance(extra_vars, str) else extra_vars
                except (json.JSONDecodeError, TypeError):
                    pass
            extra_vars_dict["_global_mode"] = True

            message_ts = parameters.get("_message_ts")
            request = PlaybookRequest.create(
                user_id=user_id,
                user_name=user_id,
                channel_id=channel_id,
                playbook=playbook_path,
                inventory=inventory_name,
                extra_vars=extra_vars_dict,
                priority=RequestPriority.NORMAL,
                message_ts=message_ts,
            )

            success, message = await self._queue.submit(request)
            host_count = inventory_info.get("total_hosts", 0)
            thread_msg = (
                f"🌍 *Global Runbook Submitted*\n\n"
                f"• Request ID: `{request.id}`\n"
                f"• Playbook: `{playbook_path}`\n"
                f"• Inventory: `{inventory_name}` ({host_count} hosts)\n"
                f"• Mode: *Global* (all hosts)\n\n"
                f"⏳ {'Starting immediately...' if success else 'Queued...'}\n"
                f"_Progress updates every {self.global_progress_interval}s in this thread_"
            )

            return MCPResult(
                status=MCPResultStatus.SUCCESS if success else MCPResultStatus.ERROR,
                message="⏳ Global runbook submitted..." if success else message,
                data={"request_id": request.id if success else None},
                thread_messages=[thread_msg] if success else [message],
            )

        # Direct execution (no queue)
        inv_id = inventory_info.get("id")
        host_count = inventory_info.get("total_hosts", 0)
        return await self._execute_playbook_global(
            playbook=playbook_path,
            inventory_id=inv_id,
            inventory_name=inventory_name,
            host_count=host_count,
            extra_vars=extra_vars if isinstance(extra_vars, dict) else None,
            channel_id=channel_id,
        )

    async def _execute_playbook_global(
        self,
        playbook: str,
        inventory_id: int,
        inventory_name: str,
        host_count: int,
        extra_vars: Optional[Dict[str, Any]] = None,
        channel_id: Optional[str] = None,
        message_ts: Optional[str] = None,
    ) -> MCPResult:
        """Execute a playbook in global mode (central inventory, long-poll with progress)."""
        try:
            # Step 1: Ensure AWX project exists and is synced
            project_id = await self._ensure_awx_project()
            if not project_id:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ Failed to create AWX project",
                    thread_messages=["❌ *Error:* Failed to create AWX project. Check AWX credentials."],
                )

            await self._sync_awx_project(project_id)
            await self._wait_for_project_sync(project_id)

            # Step 2: Create or get job template
            template_id = await self._ensure_job_template(
                playbook=playbook,
                project_id=project_id,
                inventory_id=inventory_id,
            )
            if not template_id:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ Failed to create job template",
                    thread_messages=["❌ *Error:* Failed to create job template. Check playbook path."],
                )

            # Step 3: Launch the job (skip Azure var injection for global — mixed inventory)
            extra_vars_str = None
            if extra_vars:
                import json
                extra_vars_str = json.dumps(extra_vars)

            job = await self._launch_job(template_id, extra_vars_str)
            if not job:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message="❌ Failed to launch job",
                    thread_messages=["❌ *Error:* Failed to launch AWX job."],
                )

            job_id = job.get("id")
            job_url = f"https://{self.awx_server}/#/jobs/playbook/{job_id}"

            # Step 4: Wait for completion with progress updates
            final_status, host_status_counts = await self._wait_for_global_job(
                job_id=job_id,
                channel_id=channel_id,
                message_ts=message_ts,
            )

            # Step 5: Build summary
            status_emoji = self._get_status_emoji(final_status)
            result_text = "passed" if final_status == "successful" else "failed"

            # Get elapsed time from job
            job_details = await self._get_job(job_id)
            elapsed = job_details.get("elapsed", 0) if job_details else 0
            minutes = int(elapsed) // 60
            seconds = int(elapsed) % 60
            duration_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

            ok_count = host_status_counts.get("ok", 0)
            changed_count = host_status_counts.get("changed", 0)
            failed_count = host_status_counts.get("failures", 0)
            unreachable_count = host_status_counts.get("dark", 0)
            skipped_count = host_status_counts.get("skipped", 0)
            ignored_count = host_status_counts.get("ignored", 0)
            rescued_count = host_status_counts.get("rescued", 0)

            thread_messages = []

            # Summary message
            summary = (
                f"{status_emoji} *Global Runbook {'Complete' if final_status == 'successful' else 'Failed'}*\n\n"
                f"*Playbook:* `{playbook}`\n"
                f"*Inventory:* `{inventory_name}` ({host_count} hosts)\n"
                f"*Duration:* {duration_str} | *Job ID:* `{job_id}`\n\n"
                f"*Host Summary:*\n"
                f"  OK: {ok_count} | Changed: {changed_count}\n"
                f"  Failed: {failed_count} | Unreachable: {unreachable_count}\n"
                f"  Skipped: {skipped_count} | Ignored: {ignored_count}"
            )
            if rescued_count:
                summary += f" | Rescued: {rescued_count}"

            thread_messages.append(summary)

            # Get failed host details if any failures
            if failed_count > 0 or unreachable_count > 0:
                failed_hosts_msg = await self._get_failed_hosts(job_id)
                if failed_hosts_msg:
                    thread_messages.append(failed_hosts_msg)

            # Main message update
            main_update = (
                f"*Result:* {status_emoji} {result_text} | "
                f"OK: {ok_count} | Failed: {failed_count} | "
                f"Unreachable: {unreachable_count} | Job: `{job_id}`"
            )

            return MCPResult(
                status=MCPResultStatus.SUCCESS if final_status == "successful" else MCPResultStatus.ERROR,
                message=main_update,
                data={
                    "job_id": job_id,
                    "status": final_status,
                    "host_status_counts": host_status_counts,
                },
                thread_messages=thread_messages,
                main_message_update=main_update,
                awx_url=job_url,
            )

        except Exception as e:
            logger.exception("Error executing global playbook")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"❌ Error: {str(e)}",
                thread_messages=[f"❌ *Error executing global playbook:*\n```\n{str(e)}\n```"],
            )

    async def _wait_for_global_job(
        self,
        job_id: int,
        channel_id: Optional[str] = None,
        message_ts: Optional[str] = None,
    ) -> tuple:
        """
        Wait for a global job to complete with progress updates.

        Returns:
            (status, host_status_counts) tuple
        """
        start_time = time.time()
        last_progress_time = 0
        timeout = self.global_job_timeout
        poll_interval = 10  # 10s between API polls (reduce load)

        while time.time() - start_time < timeout:
            job = await self._get_job(job_id)
            if not job:
                return ("error", {})

            status = job.get("status", "unknown")
            host_status_counts = job.get("host_status_counts", {})

            # Post progress update at configured interval
            elapsed = time.time() - start_time
            if (channel_id and self._notify_callback
                    and elapsed - last_progress_time >= self.global_progress_interval
                    and status in ("running", "pending", "waiting")):

                total_processed = sum(host_status_counts.values()) if host_status_counts else 0
                minutes = int(elapsed) // 60
                seconds = int(elapsed) % 60
                elapsed_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

                ok = host_status_counts.get("ok", 0)
                changed = host_status_counts.get("changed", 0)
                failed = host_status_counts.get("failures", 0)
                dark = host_status_counts.get("dark", 0)

                progress_msg = (
                    f"⏳ *Global run progress* ({elapsed_str}):\n"
                    f"  OK: {ok} | Changed: {changed} | Failed: {failed} | Unreachable: {dark}\n"
                    f"  Hosts processed: {total_processed}"
                )

                try:
                    await self._notify_callback(
                        channel_id=channel_id,
                        message=progress_msg,
                        thread_ts=message_ts,
                    )
                except Exception as e:
                    logger.error(f"Failed to post progress update: {e}")

                last_progress_time = elapsed

            # Check if job is done
            if status in ("successful", "failed", "error", "canceled"):
                return (status, host_status_counts)

            await asyncio.sleep(poll_interval)

        # Timeout
        job = await self._get_job(job_id)
        host_status_counts = job.get("host_status_counts", {}) if job else {}
        return ("timeout", host_status_counts)

    async def _get_failed_hosts(self, job_id: int) -> Optional[str]:
        """Get details about failed and unreachable hosts from a job."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _executor,
                self._get_failed_hosts_sync,
                job_id,
            )
        except Exception as e:
            logger.error(f"Error getting failed hosts: {e}")
            return None

    def _get_failed_hosts_sync(self, job_id: int) -> Optional[str]:
        """Synchronously get failed/unreachable hosts from AWX job host summaries."""
        url = f"https://{self.awx_server}/api/v2/jobs/{job_id}/job_host_summaries/"

        try:
            response = requests.get(
                url,
                params={"failed": "True", "page_size": 200},
                auth=(self.awx_username, self.awx_password),
                timeout=30,
                verify=False,
            )
            response.raise_for_status()
            results = response.json().get("results", [])

            failed_hosts = []
            unreachable_hosts = []

            for summary in results:
                host_name = summary.get("host_name", "unknown")
                if summary.get("dark", 0) > 0:
                    unreachable_hosts.append(host_name)
                elif summary.get("failures", 0) > 0:
                    failed_hosts.append(host_name)

            if not failed_hosts and not unreachable_hosts:
                return None

            lines = []
            if failed_hosts:
                display_count = min(len(failed_hosts), 50)
                lines.append(f"❌ *Failed hosts ({len(failed_hosts)}):*")
                for host in failed_hosts[:50]:
                    lines.append(f"  • {host}")
                if len(failed_hosts) > 50:
                    lines.append(f"  _... and {len(failed_hosts) - 50} more_")

            if unreachable_hosts:
                display_count = min(len(unreachable_hosts), 50)
                lines.append(f"🔇 *Unreachable hosts ({len(unreachable_hosts)}):*")
                for host in unreachable_hosts[:50]:
                    lines.append(f"  • {host}")
                if len(unreachable_hosts) > 50:
                    lines.append(f"  _... and {len(unreachable_hosts) - 50} more_")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Error fetching host summaries for job {job_id}: {e}")
            return f"_Could not retrieve failed host details: {str(e)}_"

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
                            f"1. Go to AWX: https://{self.awx_server}/#/credentials\n"
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
                            f"1. Go to AWX: https://{self.awx_server}/#/credentials\n"
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
        url = f"https://{self.awx_server}/api/v2/credentials/{cred_id}/"
        response = requests.get(
            url,
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
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
        type_url = f"https://{self.awx_server}/api/v2/credential_types/"
        type_response = requests.get(
            type_url,
            params={"name": "Machine"},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
        )
        type_response.raise_for_status()
        types = type_response.json().get("results", [])
        if not types:
            return []
        machine_type_id = types[0]["id"]

        # Get credentials of that type
        url = f"https://{self.awx_server}/api/v2/credentials/"
        response = requests.get(
            url,
            params={"credential_type": machine_type_id},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
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

    def _diagnose_failure(self, output: str, raw_output: str = "") -> tuple:
        """
        Analyze job output and return diagnosis and solution.

        Returns:
            (diagnosis, solution) tuple
        """
        combined_output = f"{output}\n{raw_output}".lower()

        # Error patterns ordered by specificity (most specific first)
        error_patterns = [
            # Project sync / SCM update failure (job never ran)
            {
                "patterns": ["previous task failed", "project_update", "authentication failed", "anonymous access denied"],
                "diagnosis": "AWX project sync failed (Git authentication error)",
                "solution": (
                    "AWX could not pull the latest playbooks from GitHub. Possible causes:\n"
                    "• GitHub token expired or invalid\n"
                    "• SCM credential username is empty (must be 'git')\n"
                    "• GitHub Enterprise is unreachable from AWX\n\n"
                    "To fix:\n"
                    "1. Check AWX project sync status in the AWX UI\n"
                    "2. Update the GitHub token: `/awx setup ssh`\n"
                    "3. Verify the SCM credential has username='git' and a valid token\n"
                    "4. Try syncing the project manually in AWX"
                ),
            },
            # APT/Package manager errors
            {
                "patterns": ["apt cache", "apt-get update", "failed to update apt", "apt update"],
                "diagnosis": "Package manager failure",
                "solution": (
                    "APT cache update failed. Possible causes:\n"
                    "• Network access to apt repositories is blocked\n"
                    "• apt is locked by another process\n"
                    "• Repository configuration issues in /etc/apt/sources.list\n"
                    "• DNS resolution issues for apt mirrors\n\n"
                    "To fix:\n"
                    "1. SSH to the host and run: `sudo apt-get update`\n"
                    "2. Check if apt lock exists: `sudo lsof /var/lib/apt/lists/lock`\n"
                    "3. Verify network access: `curl -I http://archive.ubuntu.com`"
                ),
            },
            # DNS resolution errors
            {
                "patterns": ["could not resolve host", "name or service not known", "temporary failure in name resolution"],
                "diagnosis": "DNS resolution failure",
                "solution": (
                    "The host cannot resolve DNS names. Possible causes:\n"
                    "• DNS server is unreachable\n"
                    "• /etc/resolv.conf misconfigured\n"
                    "• Network connectivity issues\n\n"
                    "To fix:\n"
                    "1. Check DNS config: `cat /etc/resolv.conf`\n"
                    "2. Test DNS: `nslookup google.com`\n"
                    "3. Verify network: `ping 8.8.8.8`"
                ),
            },
            # SSH/Connection errors - host unreachable
            {
                "patterns": ["unreachable", "connection refused", "connection timed out", "no route to host"],
                "diagnosis": "Host unreachable - connectivity failure",
                "solution": (
                    "The playbook cannot connect to the target hosts. Possible causes:\n"
                    "• Host is down or unreachable\n"
                    "• Firewall blocking SSH (port 22)\n"
                    "• Wrong IP address in inventory\n\n"
                    "To fix:\n"
                    "1. Verify host is running and reachable\n"
                    "2. Check network connectivity\n"
                    "3. Verify SSH port is open"
                ),
            },
            # SSH authentication errors
            {
                "patterns": ["permission denied (publickey", "authentication failed", "no supported authentication"],
                "diagnosis": "SSH authentication failure",
                "solution": (
                    "SSH key authentication failed. Possible causes:\n"
                    "• SSH credential not configured in AWX\n"
                    "• Wrong SSH key for this host\n"
                    "• User not authorized on target host\n\n"
                    "To fix:\n"
                    "1. Check AWX credential configuration\n"
                    "2. Verify SSH key is added to target host\n"
                    "3. Run `setup ssh` to check credential status"
                ),
            },
            # Incorrect sudo password (Azure hosts)
            {
                "patterns": ["incorrect sudo password", "sorry, try again", "sudo: 1 incorrect password attempt"],
                "diagnosis": "Incorrect sudo password",
                "solution": (
                    "The sudo password provided is incorrect. For Azure hosts (10.253.x.x):\n"
                    "• The AZURE_SUDO_PASSWORD environment variable may be wrong\n"
                    "• The password may have changed on the target host\n\n"
                    "To fix:\n"
                    "1. Verify the correct sudo password for vivoxops user\n"
                    "2. Update AZURE_SUDO_PASSWORD in K8s secrets\n"
                    "3. Restart the slack-mcp-agent pod"
                ),
            },
            # Sudo/Permission errors
            {
                "patterns": ["sudo: a password is required", "permission denied", "requires become", "must be run as root"],
                "diagnosis": "Insufficient permissions",
                "solution": (
                    "The task requires elevated privileges. Possible causes:\n"
                    "• User doesn't have sudo access\n"
                    "• become/sudo not enabled in playbook\n"
                    "• sudo password required but not provided\n\n"
                    "To fix:\n"
                    "1. Verify user has sudo access on target\n"
                    "2. Check playbook has `become: yes`\n"
                    "3. Configure sudo password in AWX credentials"
                ),
            },
            # File/Path errors
            {
                "patterns": ["no such file or directory", "file not found", "path does not exist"],
                "diagnosis": "File or path not found",
                "solution": (
                    "A required file or path doesn't exist. Possible causes:\n"
                    "• Playbook path is incorrect\n"
                    "• Required files not present on target\n"
                    "• Template or variable file missing\n\n"
                    "To fix:\n"
                    "1. Verify playbook path in GitHub repo\n"
                    "2. Check required files exist on target host\n"
                    "3. Review playbook for hardcoded paths"
                ),
            },
            # Disk space errors
            {
                "patterns": ["no space left on device", "disk quota exceeded", "cannot allocate memory"],
                "diagnosis": "Disk space or memory issue",
                "solution": (
                    "The host has insufficient resources. Possible causes:\n"
                    "• Disk is full\n"
                    "• Disk quota exceeded\n"
                    "• Out of memory\n\n"
                    "To fix:\n"
                    "1. Check disk space: `df -h`\n"
                    "2. Clean up old files/logs\n"
                    "3. Check memory: `free -m`"
                ),
            },
            # Timeout errors
            {
                "patterns": ["timeout", "timed out", "deadline exceeded"],
                "diagnosis": "Operation timed out",
                "solution": (
                    "The operation took too long. Possible causes:\n"
                    "• Slow network connection\n"
                    "• Large download/operation\n"
                    "• Host under heavy load\n\n"
                    "To fix:\n"
                    "1. Increase task timeout in playbook\n"
                    "2. Check host resource usage\n"
                    "3. Run task manually to diagnose"
                ),
            },
            # Docker errors
            {
                "patterns": ["docker daemon", "docker: error", "container", "image not found", "pull access denied"],
                "diagnosis": "Docker operation failed",
                "solution": (
                    "A Docker operation failed. Possible causes:\n"
                    "• Docker daemon not running\n"
                    "• Image not found or access denied\n"
                    "• Container configuration issue\n\n"
                    "To fix:\n"
                    "1. Check Docker status: `systemctl status docker`\n"
                    "2. Verify image exists and credentials are correct\n"
                    "3. Check container logs: `docker logs <container>`"
                ),
            },
            # Service errors
            {
                "patterns": ["service", "systemctl", "failed to start", "failed to enable"],
                "diagnosis": "Service operation failed",
                "solution": (
                    "A service operation failed. Possible causes:\n"
                    "• Service configuration error\n"
                    "• Missing dependencies\n"
                    "• Port already in use\n\n"
                    "To fix:\n"
                    "1. Check service status: `systemctl status <service>`\n"
                    "2. View logs: `journalctl -u <service>`\n"
                    "3. Verify configuration files"
                ),
            },
        ]

        # Check each pattern
        for error in error_patterns:
            for pattern in error["patterns"]:
                if pattern in combined_output:
                    return (error["diagnosis"], error["solution"])

        # Default diagnosis if no pattern matches
        return (
            "Unknown failure",
            (
                "The playbook failed for an unrecognized reason.\n\n"
                "To diagnose:\n"
                "1. Review the full output above\n"
                "2. Check AWX job details for more context\n"
                "3. Run the playbook manually with verbose mode: `-vvv`"
            )
        )

    def _is_azure_inventory(self, inventory_id: int) -> bool:
        """
        Check if inventory contains Azure hosts (IPs starting with 10.253.x.x).

        Azure hosts require different SSH settings:
        - Username: vivoxops (instead of root)
        - Become: sudo with password
        """
        try:
            url = f"https://{self.awx_server}/api/v2/inventories/{inventory_id}/hosts/"
            response = requests.get(
                url,
                auth=(self.awx_username, self.awx_password),
                timeout=30,
                verify=False
            )
            response.raise_for_status()

            hosts = response.json().get("results", [])
            for host in hosts:
                # Check host variables for ansible_host IP
                variables = host.get("variables", "")
                if isinstance(variables, str):
                    try:
                        import json
                        variables = json.loads(variables) if variables else {}
                    except json.JSONDecodeError:
                        variables = {}

                # Check ansible_host or the host name itself
                ansible_host = variables.get("ansible_host", "")
                host_name = host.get("name", "")

                # Check if IP starts with 10.253
                if ansible_host.startswith("10.253.") or host_name.startswith("10.253."):
                    logger.info(f"Detected Azure host in inventory {inventory_id}: {host_name} ({ansible_host})")
                    return True

            return False
        except Exception as e:
            logger.error(f"Error checking Azure inventory: {e}")
            return False

    def _get_azure_extra_vars(self) -> Dict[str, Any]:
        """Get extra vars for Azure hosts (vivoxops user with sudo)."""
        return {
            "ansible_user": "vivoxops",
            "ansible_become": True,
            "ansible_become_method": "sudo",
            "ansible_become_password": self.azure_sudo_password,
        }

    def _format_ansible_output(self, output: str) -> str:
        """Parse and format Ansible output for better Slack readability."""
        import re
        import json

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
        last_was_fatal = False

        for line in lines:
            line = line.strip()

            # Handle "...ignoring" on its own line after a fatal
            if line == '...ignoring' and last_was_fatal:
                if formatted_lines:
                    formatted_lines.append(f"   (ignored)")
                last_was_fatal = False
                continue

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
                last_was_fatal = False
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
                    last_was_fatal = True
                    # Include error message with useful detail
                    formatted_lines.append(f"❌ {host_name}: {current_task}")

                    # Try to parse the JSON result from the fatal line
                    json_match = re.search(r'=>\s*(\{.+)', line)
                    if json_match:
                        try:
                            result_data = json.loads(json_match.group(1))
                            # Build useful error info from available fields
                            error_parts = []

                            # Show the command that was run
                            cmd = result_data.get("cmd")
                            if cmd:
                                if isinstance(cmd, list):
                                    cmd = " ".join(cmd)
                                error_parts.append(f"Command: {str(cmd)[:150]}")

                            # stderr is usually the most useful
                            stderr = result_data.get("stderr", "").strip()
                            if stderr:
                                # Take last meaningful line of stderr
                                stderr_lines = [l.strip() for l in stderr.split('\n') if l.strip()]
                                if stderr_lines:
                                    error_parts.append(f"stderr: {stderr_lines[-1][:200]}")

                            # msg as fallback (but skip generic "non-zero return code")
                            msg = result_data.get("msg", "").strip()
                            if msg and msg != "non-zero return code":
                                error_parts.append(f"Error: {msg[:200]}")
                            elif msg == "non-zero return code":
                                rc = result_data.get("rc", "?")
                                error_parts.append(f"Exit code: {rc}")

                            # stdout can have useful info too
                            stdout = result_data.get("stdout", "").strip()
                            if stdout and not stderr:
                                stdout_lines = [l.strip() for l in stdout.split('\n') if l.strip()]
                                if stdout_lines:
                                    error_parts.append(f"Output: {stdout_lines[-1][:200]}")

                            for part in error_parts:
                                formatted_lines.append(f"   {part}")

                            if not error_parts:
                                formatted_lines.append(f"   Error: {msg or 'unknown'}")
                        except (json.JSONDecodeError, TypeError):
                            # Fall back to regex extraction
                            error_match = re.search(r'"msg":\s*"(.+?)"', line)
                            if error_match:
                                formatted_lines.append(f"   Error: {error_match.group(1)[:200]}")
                    else:
                        # No JSON found, try simple regex
                        error_match = re.search(r'"msg":\s*"(.+?)"', line)
                        if error_match:
                            formatted_lines.append(f"   Error: {error_match.group(1)[:200]}")

                    # Check if task was ignored (ignore_errors: yes)
                    if '...ignoring' in line:
                        formatted_lines.append(f"   (ignored)")
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
        url = f"https://{self.awx_server}/api/v2/inventories/"
        response = requests.get(
            url,
            params={"name": name},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
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
        url = f"https://{self.awx_server}/api/v2/projects/"
        response = requests.get(
            url,
            params={"name": project_name},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
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
                    update_url = f"https://{self.awx_server}/api/v2/projects/{project_id}/"
                    requests.patch(
                        update_url,
                        json={"credential": scm_credential_id},
                        auth=(self.awx_username, self.awx_password),
                        timeout=30
                    )
                    logger.info(f"Updated project {project_id} with SCM credential {scm_credential_id}")

            return project_id

        # Get organization ID
        org_url = f"https://{self.awx_server}/api/v2/organizations/"
        org_response = requests.get(
            org_url,
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
        )
        org_response.raise_for_status()
        org_id = org_response.json().get("results", [{}])[0].get("id", 1)

        # Create or get SCM credential for GitHub
        scm_credential_id = None
        if self.github_token:
            scm_credential_id = self._ensure_scm_credential(org_id)

        # Create project
        # Build the full GitHub URL for SCM
        if "api.github.com" in self.github_api_url:
            github_base = "https://github.com"
        else:
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
            timeout=30,
            verify=False
        )
        response.raise_for_status()

        return response.json().get("id")

    def _ensure_scm_credential(self, org_id: int) -> Optional[int]:
        """Create or get SCM credential for GitHub authentication."""
        credential_name = self._scm_credential_name

        # Check if credential exists
        url = f"https://{self.awx_server}/api/v2/credentials/"
        response = requests.get(
            url,
            params={"name": credential_name},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        if results:
            # Update existing credential with current token
            cred_id = results[0]["id"]
            update_url = f"https://{self.awx_server}/api/v2/credentials/{cred_id}/"
            requests.patch(
                update_url,
                json={"inputs": {"username": "git", "password": self.github_token}},
                auth=(self.awx_username, self.awx_password),
                timeout=30,
                verify=False
            )
            return cred_id

        # Get credential type ID for "Source Control"
        cred_type_url = f"https://{self.awx_server}/api/v2/credential_types/"
        cred_type_response = requests.get(
            cred_type_url,
            params={"name": "Source Control"},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
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
            timeout=30,
            verify=False
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
        url = f"https://{self.awx_server}/api/v2/projects/{project_id}/update/"
        try:
            response = requests.post(
                url,
                auth=(self.awx_username, self.awx_password),
                timeout=30,
                verify=False
            )
            # 202 Accepted is success for async operations
            return response.status_code in [200, 201, 202]
        except Exception as e:
            logger.error(f"Error syncing project: {e}")
            return False

    async def _wait_for_project_sync(self, project_id: int, timeout: int = 60) -> bool:
        """Wait for AWX project sync to complete."""
        import time
        start = time.time()
        url = f"https://{self.awx_server}/api/v2/projects/{project_id}/"
        while time.time() - start < timeout:
            try:
                loop = asyncio.get_event_loop()
                status = await loop.run_in_executor(
                    _executor,
                    lambda: requests.get(
                        url,
                        auth=(self.awx_username, self.awx_password),
                        timeout=30,
                        verify=False
                    ).json().get("status", "unknown")
                )
                if status == "successful":
                    logger.info(f"Project {project_id} sync completed")
                    return True
                if status in ("failed", "error", "canceled"):
                    logger.error(f"Project {project_id} sync failed with status: {status}")
                    return False
                # Still running/pending/waiting
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Error checking project sync status: {e}")
                return False
        logger.warning(f"Project {project_id} sync timed out after {timeout}s")
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
        url = f"https://{self.awx_server}/api/v2/job_templates/"
        response = requests.get(
            url,
            params={"name": template_name},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        if results:
            template = results[0]
            template_id = template["id"]

            # Update inventory and execution environment if different
            update_payload = {}
            if template.get("inventory") != inventory_id:
                update_payload["inventory"] = inventory_id

            # Ensure execution environment is set
            if self.awx_execution_environment_id:
                ee_id = int(self.awx_execution_environment_id)
                current_ee = template.get("execution_environment")
                if current_ee != ee_id:
                    update_payload["execution_environment"] = ee_id
                    logger.info(f"Updating template {template_id} to use EE {ee_id}")

            if update_payload:
                update_url = f"https://{self.awx_server}/api/v2/job_templates/{template_id}/"
                requests.patch(
                    update_url,
                    json=update_payload,
                    auth=(self.awx_username, self.awx_password),
                    timeout=30,
                    verify=False
                )

            # Ensure credential is attached (AWX uses separate endpoint)
            if self.awx_credential_id:
                existing_creds = template.get("summary_fields", {}).get("credentials", [])
                cred_ids = [c.get("id") for c in existing_creds]
                cred_id = int(self.awx_credential_id)

                if cred_id not in cred_ids:
                    cred_url = f"https://{self.awx_server}/api/v2/job_templates/{template_id}/credentials/"
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

        # Add execution environment if configured
        if self.awx_execution_environment_id:
            payload["execution_environment"] = int(self.awx_execution_environment_id)
            logger.info(f"Creating template with EE {self.awx_execution_environment_id}")

        logger.info(f"Creating job template: {payload}")
        response = requests.post(
            url,
            json=payload,
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
        )
        if response.status_code >= 400:
            logger.error(f"Job template creation failed ({response.status_code}): {response.text}")
        response.raise_for_status()

        template_id = response.json().get("id")

        # Attach credential via separate endpoint (AWX requires this)
        if self.awx_credential_id and template_id:
            cred_url = f"https://{self.awx_server}/api/v2/job_templates/{template_id}/credentials/"
            try:
                requests.post(
                    cred_url,
                    json={"id": int(self.awx_credential_id)},
                    auth=(self.awx_username, self.awx_password),
                    timeout=30,
                    verify=False
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
        url = f"https://{self.awx_server}/api/v2/job_templates/{template_id}/launch/"

        payload = {}
        if extra_vars:
            payload["extra_vars"] = extra_vars

        response = requests.post(
            url,
            json=payload,
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
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
        url = f"https://{self.awx_server}/api/v2/jobs/{job_id}/"

        response = requests.get(
            url,
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
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
        url = f"https://{self.awx_server}/api/v2/jobs/{job_id}/stdout/"

        response = requests.get(
            url,
            params={"format": "txt"},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
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
        url = f"https://{self.awx_server}/api/v2/jobs/"

        response = requests.get(
            url,
            params={"order_by": "-created", "page_size": limit},
            auth=(self.awx_username, self.awx_password),
            timeout=30,
            verify=False
        )
        response.raise_for_status()

        return response.json().get("results", [])

    async def health_check(self) -> bool:
        """Check if AWX and GitHub are reachable."""
        try:
            # Check AWX
            response = requests.get(
                f"https://{self.awx_server}/api/v2/ping/",
                auth=(self.awx_username, self.awx_password),
                timeout=10,
                verify=False
            )
            return response.status_code == 200
        except Exception:
            return False
