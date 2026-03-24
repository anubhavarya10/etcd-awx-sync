"""Terraform Manager MCP - Core logic for Terraform resource scaling."""

import os
import asyncio
import logging
from typing import Any, Dict, List, Optional

from .git_client import GitClient
from .tfc_client import TFCClient
from .tf_parser import (
    parse_variables,
    set_variable,
    set_bool_variable,
    find_matching_variables,
)
from .role_map import (
    get_tf_variable,
    get_required_feature_flags,
    is_valid_role,
    list_roles,
)
from .pending_ops import PendingOpsStore, PendingOp
from .openstack_client import OpenStackClient

logger = logging.getLogger(__name__)


class MCPResultStatus:
    SUCCESS = "success"
    ERROR = "error"
    PENDING = "pending"


class MCPResult:
    def __init__(self, status: str, message: str, data: Optional[Dict[str, Any]] = None):
        self.status = status
        self.message = message
        self.data = data or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


class TerraformManagerMCP:
    """
    Terraform Manager MCP for scaling OpenStack resources via Slack.

    Actions:
    - scale-resource: Add/remove/set resource count, commit, push, trigger TF plan
    - show-domain: Show current resource counts from .tf file
    - show-status: List pending TFC operations
    - confirm-apply: Apply a pending TF plan
    - cancel-run: Discard a pending TF plan
    - show-help: Show usage help
    """

    def __init__(self):
        self.git_client = GitClient()
        self.tfc_client = TFCClient()
        self.pending_ops = PendingOpsStore()
        self.openstack_client = OpenStackClient()

    @property
    def name(self) -> str:
        return "tf-manager"

    @property
    def description(self) -> str:
        return "Scale Terraform-managed OpenStack resources via Slack"

    def get_actions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "scale-resource",
                "description": "Add, remove, or set resource count for a role in a domain",
                "parameters": [
                    {"name": "role", "type": "string", "required": True, "description": "Server role (e.g., mim, mphpp, ts)"},
                    {"name": "domain", "type": "string", "required": True, "description": "Domain name (e.g., aptus2, pubwxp)"},
                    {"name": "operation", "type": "string", "required": True, "description": "Operation: add, remove, or set"},
                    {"name": "count", "type": "integer", "required": True, "description": "Number of instances to add/remove, or target count for set"},
                    {"name": "region", "type": "string", "required": False, "description": "Region for mphpp (e.g., bos_1, bos_2, ams_1, sin_1)"},
                    {"name": "_confirmed", "type": "boolean", "required": False, "description": "Internal: set to true after destructive op confirmation"},
                ],
                "examples": [
                    "add 2 mphpp to aptus2",
                    "set mim 5 in pubwxp",
                    "remove 1 ts from lionxp",
                ],
            },
            {
                "name": "show-domain",
                "description": "Show current resource counts for a domain from the .tf file",
                "parameters": [
                    {"name": "domain", "type": "string", "required": True, "description": "Domain name"},
                ],
                "examples": ["show aptus2", "what's in pubwxp"],
            },
            {
                "name": "show-status",
                "description": "List pending Terraform Cloud operations awaiting confirmation",
                "parameters": [],
                "examples": ["status", "pending ops"],
            },
            {
                "name": "confirm-apply",
                "description": "Apply a pending Terraform plan",
                "parameters": [
                    {"name": "run_id", "type": "string", "required": True, "description": "TFC run ID to apply"},
                ],
                "examples": ["confirm run-abc123"],
            },
            {
                "name": "cancel-run",
                "description": "Discard a pending Terraform plan",
                "parameters": [
                    {"name": "run_id", "type": "string", "required": True, "description": "TFC run ID to discard"},
                ],
                "examples": ["cancel run-abc123"],
            },
            {
                "name": "show-help",
                "description": "Show usage help for the /tf command",
                "parameters": [],
                "examples": ["help"],
            },
        ]

    async def execute(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str = "",
        channel_id: str = "",
    ) -> MCPResult:
        logger.info(f"TerraformManager executing {action} with params: {parameters}")

        try:
            if action == "scale-resource":
                return await self._handle_scale_resource(parameters, user_id, channel_id)
            elif action == "show-domain":
                return await self._handle_show_domain(parameters)
            elif action == "show-status":
                return await self._handle_show_status()
            elif action == "confirm-apply":
                return await self._handle_confirm_apply(parameters, user_id)
            elif action == "cancel-run":
                return await self._handle_cancel_run(parameters, user_id)
            elif action == "show-help":
                return self._handle_show_help()
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

    async def _handle_scale_resource(
        self,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        role = parameters.get("role", "").strip().lower()
        domain = parameters.get("domain", "").strip().lower()
        operation = parameters.get("operation", "").strip().lower()
        count = int(parameters.get("count", 0))
        region = parameters.get("region", "").strip().lower()
        confirmed = parameters.get("_confirmed", False)

        # Validate inputs
        if not role or not domain or not operation or count <= 0:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Missing required parameters: role, domain, operation, count (> 0)",
            )

        if operation not in ("add", "remove", "set"):
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Invalid operation: `{operation}`. Use `add`, `remove`, or `set`.",
            )

        if not is_valid_role(role):
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Unknown role: `{role}`\n\nSupported roles: {', '.join(list_roles())}",
            )

        # Clone/pull the repo
        try:
            self.git_client.clone_or_pull()
        except Exception as e:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Git error: {str(e)}",
            )

        # Read the .tf file
        tf_path = self.git_client.get_file_path(domain)
        try:
            content = self.git_client.read_file(tf_path)
        except FileNotFoundError:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"No Terraform file found for domain `{domain}` at `production/{domain}/xmpp.tf`",
            )

        # Resolve the TF variable name
        tf_var = get_tf_variable(role)
        if tf_var is None and role == "mphpp":
            # mphpp requires region detection
            tf_var, error = self._resolve_mphpp_variable(content, region)
            if error:
                return MCPResult(status=MCPResultStatus.ERROR, message=error)
        elif tf_var is None:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Cannot map role `{role}` to a Terraform variable",
            )

        # Get current value
        variables = parse_variables(content)
        current_value = variables.get(tf_var)
        if current_value is None or not isinstance(current_value, int):
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Variable `{tf_var}` not found in `production/{domain}/xmpp.tf`",
            )

        # Calculate new value
        if operation == "add":
            new_value = current_value + count
        elif operation == "remove":
            new_value = max(0, current_value - count)
        elif operation == "set":
            new_value = count
        else:
            new_value = current_value

        if new_value == current_value:
            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message=f"`{tf_var}` is already `{current_value}` in `{domain}` — no change needed.",
            )

        # Check for destructive operation (scaling down)
        is_destructive = new_value < current_value
        if is_destructive and not confirmed:
            return MCPResult(
                status=MCPResultStatus.PENDING,
                message=(
                    f":warning: *Destructive Operation*\n\n"
                    f"This will *reduce* `{tf_var}` from `{current_value}` to `{new_value}` in `{domain}`.\n"
                    f"Terraform will *destroy* {current_value - new_value} instance(s).\n\n"
                    f"To confirm, run:\n"
                    f"`/tf {operation} {count} {role} {'from' if operation == 'remove' else 'in'} {domain} --confirm`"
                ),
                data={"needs_confirmation": True},
            )

        # Check feature flags
        required_flags = get_required_feature_flags(role)
        flag_warnings = []
        for flag in required_flags:
            flag_value = variables.get(flag)
            if flag_value is False:
                flag_warnings.append(
                    f":warning: Feature flag `{flag}` is currently `false`. "
                    f"Role `{role}` requires `{flag} = true` to function."
                )

        # Modify the .tf file
        try:
            new_content, old_val = set_variable(content, tf_var, new_value)
        except ValueError as e:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Failed to modify variable: {str(e)}",
            )

        # Write, commit, push
        self.git_client.write_file(tf_path, new_content)
        commit_msg = f"Scale {tf_var} from {current_value} to {new_value} for {domain}"
        try:
            commit_hash = self.git_client.commit_and_push(tf_path, commit_msg)
        except Exception as e:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Git push failed: {str(e)}",
            )

        # Get TFC workspace
        try:
            workspace = await self.tfc_client.get_workspace(domain)
            workspace_id = workspace["id"]
            workspace_name = workspace["attributes"]["name"]
        except Exception as e:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"Git commit pushed ({commit_hash}), but failed to find TFC workspace "
                    f"`vivox-ops-openstack-{domain}`: {str(e)}"
                ),
            )

        # Wait for VCS-triggered run to appear (GitHub webhook auto-creates it)
        logger.info(f"Git pushed. Waiting for VCS-triggered run in workspace {workspace_name}...")
        try:
            run_data = await self.tfc_client.find_vcs_run(workspace_id, commit_msg)
            run_id = run_data["id"]
        except TimeoutError:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"Git pushed ({commit_hash}), but no TFC run was triggered.\n"
                    f"The GitHub webhook may not have fired. Check the workspace in TFC."
                ),
                data={"commit": commit_hash},
            )

        # Wait for plan to complete (blocking — up to 10 min)
        logger.info(f"Found run {run_id}. Waiting for plan to complete...")
        try:
            run_result = await self.tfc_client.wait_for_plan(run_id)
            plan_status = run_result["attributes"]["status"]
        except TimeoutError:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"Git pushed ({commit_hash}), TFC run ({run_id}) found, "
                    f"but plan timed out after 10 minutes.\n"
                    f"Check the run in Terraform Cloud."
                ),
                data={"run_id": run_id, "commit": commit_hash},
            )

        # Check if plan failed
        if plan_status in ("errored", "canceled", "discarded", "force_canceled", "policy_override"):
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"TFC plan failed with status: `{plan_status}`\n"
                    f"*Run:* {run_id}\n"
                    f"*Commit:* {commit_hash}\n"
                    f"Check the Terraform Cloud UI for details."
                ),
                data={"run_id": run_id, "status": plan_status},
            )

        # Get plan summary (resource counts)
        plan_summary = ""
        try:
            plan_rel = run_result.get("relationships", {}).get("plan", {}).get("data", {})
            plan_id = plan_rel.get("id", "")
            if plan_id:
                plan_data = await self.tfc_client.get_plan(plan_id)
                additions = plan_data["attributes"].get("resource-additions", 0)
                changes = plan_data["attributes"].get("resource-changes", 0)
                destructions = plan_data["attributes"].get("resource-destructions", 0)
                plan_summary = f"+{additions} additions, ~{changes} changes, -{destructions} destructions"
        except Exception as e:
            logger.warning(f"Failed to get plan details: {e}")
            plan_summary = "Plan details unavailable"

        # Store pending operation with plan summary
        self.pending_ops.add(PendingOp(
            run_id=run_id,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            domain=domain,
            role=role,
            tf_var=tf_var,
            old_value=current_value,
            new_value=new_value,
            plan_summary=plan_summary,
            user_id=user_id,
            channel_id=channel_id,
        ))

        # Build response with plan summary and confirm/cancel options
        flag_warning_str = "\n".join(flag_warnings) + "\n" if flag_warnings else ""
        message = (
            f":clipboard: *TF Plan Complete*\n\n"
            f"*Domain:* {domain}\n"
            f"*Change:* `{tf_var}`: `{current_value}` -> `{new_value}`\n"
            f"*Plan:* {plan_summary}\n"
            f"*Commit:* {commit_hash}\n"
            f"*Run:* {run_id}\n"
            f"{flag_warning_str}\n"
            f"To apply this change:\n"
            f"  `/tf confirm {run_id}`\n\n"
            f"To discard:\n"
            f"  `/tf cancel {run_id}`"
        )

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=message,
            data={
                "run_id": run_id,
                "workspace_id": workspace_id,
                "tf_var": tf_var,
                "old_value": current_value,
                "new_value": new_value,
                "commit": commit_hash,
                "plan_summary": plan_summary,
                "plan_status": plan_status,
            },
        )

    def _resolve_mphpp_variable(self, content: str, region: str) -> tuple:
        """
        Resolve the correct mphpp TF variable from the .tf file.

        Returns (tf_var_name, error_message). error_message is None on success.
        """
        matching = find_matching_variables(content, "mphpp_ostack")

        if not matching:
            return None, "No `mphpp_ostack_*` variables found in the .tf file."

        if len(matching) == 1:
            # Only one mphpp variable — use it
            return list(matching.keys())[0], None

        # Multiple mphpp variables — need region
        if region:
            target = f"mphpp_ostack_{region}"
            if target in matching:
                return target, None
            else:
                available = ", ".join(f"`{k}`" for k in sorted(matching.keys()))
                return None, (
                    f"Region `{region}` not found. "
                    f"Available mphpp variables: {available}"
                )

        # No region specified, multiple options
        available = ", ".join(
            f"`{k}` (={v})" for k, v in sorted(matching.items())
        )
        regions = ", ".join(
            f"`{k.replace('mphpp_ostack_', '')}`" for k in sorted(matching.keys())
        )
        return None, (
            f"Multiple mphpp regions found: {available}\n\n"
            f"Specify the region: `/tf add <count> mphpp <region> to <domain>`\n"
            f"Available regions: {regions}"
        )

    async def _handle_show_domain(self, parameters: Dict[str, Any]) -> MCPResult:
        domain = parameters.get("domain", "").strip().lower()
        if not domain:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Missing required parameter: `domain`",
            )

        try:
            self.git_client.clone_or_pull()
        except Exception as e:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Git error: {str(e)}",
            )

        tf_path = self.git_client.get_file_path(domain)
        try:
            content = self.git_client.read_file(tf_path)
        except FileNotFoundError:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"No Terraform file found for domain `{domain}`",
            )

        variables = parse_variables(content)

        # Separate int and bool variables
        int_vars = {k: v for k, v in variables.items() if isinstance(v, int)}
        bool_vars = {k: v for k, v in variables.items() if isinstance(v, bool)}

        lines = [f"*Resource counts for `{domain}`:*\n"]

        if int_vars:
            lines.append("*Instances:*")
            for name, value in sorted(int_vars.items()):
                lines.append(f"  `{name}` = `{value}`")

        if bool_vars:
            lines.append("\n*Feature Flags:*")
            for name, value in sorted(bool_vars.items()):
                icon = ":white_check_mark:" if value else ":x:"
                lines.append(f"  {icon} `{name}` = `{value}`")

        if not int_vars and not bool_vars:
            lines.append("_No variables found in the .tf file._")

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(lines),
            data={"domain": domain, "variables": {k: str(v) for k, v in variables.items()}},
        )

    async def _handle_show_status(self) -> MCPResult:
        pending = self.pending_ops.list_all()

        if not pending:
            return MCPResult(
                status=MCPResultStatus.SUCCESS,
                message="No pending Terraform operations.",
            )

        lines = [f"*Pending TF Operations ({len(pending)}):*\n"]
        for op in pending:
            import time
            age_min = int((time.time() - op.created_at) / 60)
            lines.append(
                f"  *Run:* {op.run_id}\n"
                f"  *Domain:* {op.domain}  |  {op.tf_var}: {op.old_value} -> {op.new_value}\n"
                f"  *Age:* {age_min}m  |  *User:* <@{op.user_id}>\n"
                f"  /tf confirm {op.run_id}\n"
                f"  /tf cancel {op.run_id}\n"
            )

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message="\n".join(lines),
            data={"count": len(pending)},
        )

    async def _handle_confirm_apply(
        self,
        parameters: Dict[str, Any],
        user_id: str,
    ) -> MCPResult:
        run_id = parameters.get("run_id", "").strip()
        if not run_id:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Missing required parameter: `run_id`",
            )

        # Check pending ops store
        op = self.pending_ops.get(run_id)
        if not op:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=(
                    f"No pending operation found for run `{run_id}`.\n"
                    f"It may have expired (30 min) or already been processed.\n"
                    f"Use `/tf status` to see current pending operations."
                ),
            )

        # Check if plan is ready before applying
        try:
            run_data = await self.tfc_client.get_run(run_id)
            run_status = run_data["attributes"]["status"]
        except Exception as e:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Failed to check run status: {str(e)}",
            )

        # If still planning, wait for it (up to 10 min)
        if run_status in ("pending", "plan_queued", "planning", "cost_estimating", "policy_checking"):
            try:
                run_data = await self.tfc_client.wait_for_plan(run_id)
                run_status = run_data["attributes"]["status"]
            except TimeoutError:
                return MCPResult(
                    status=MCPResultStatus.ERROR,
                    message=f"Plan is still running for {run_id}. Try again in a few minutes.",
                    data={"run_id": run_id},
                )

        # Check if plan succeeded
        if run_status in ("errored", "canceled", "discarded", "force_canceled"):
            self.pending_ops.remove(run_id)
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Run {run_id} is in status: {run_status}. Cannot apply.",
                data={"run_id": run_id, "status": run_status},
            )

        if run_status not in ("planned", "planned_and_finished", "cost_estimated", "policy_checked", "policy_soft_failed"):
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Run {run_id} is not ready for apply (status: {run_status}). Wait for planning to complete.",
                data={"run_id": run_id, "status": run_status},
            )

        # Apply the run
        user_name = parameters.get("user_name", user_id)
        try:
            await self.tfc_client.apply_run(run_id, comment=f"Applied by {user_name} via Slack")
        except Exception as e:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Failed to apply run {run_id}: {str(e)}",
            )

        # Wait for apply to complete
        try:
            run_result = await self.tfc_client.wait_for_apply(run_id)
            run_status = run_result["attributes"]["status"]
        except TimeoutError:
            self.pending_ops.remove(run_id)
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Apply timed out for run {run_id}. Check the Terraform Cloud UI for status.",
                data={"run_id": run_id},
            )

        if run_status != "applied":
            self.pending_ops.remove(run_id)
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Apply failed with status: {run_status} for run {run_id}.",
                data={"run_id": run_id, "status": run_status},
            )

        # Query OpenStack for actual server IPs
        ip_info = ""
        try:
            servers = await self.openstack_client.get_servers(op.role, op.domain)
            if servers:
                parsed = self.openstack_client.parse_server_ips(servers)
                if parsed:
                    lines = []
                    for s in parsed:
                        ip_str = ", ".join(s["ips"]) if s["ips"] else "no IP"
                        lines.append(f"  `{s['name']}` — {ip_str} ({s['status']})")
                    ip_info = f"\n\n*{op.role} servers in {op.domain} ({len(parsed)}):*\n" + "\n".join(lines)
                else:
                    ip_info = f"\n\n:warning: No {op.role} servers found in OpenStack for `{op.domain}`. Servers may still be provisioning."
            else:
                ip_info = f"\n\n:warning: Could not query OpenStack for {op.role} servers. Check SSH connectivity."
        except Exception as e:
            logger.warning(f"Failed to query OpenStack: {e}")
            ip_info = f"\n\n:warning: Could not query OpenStack for server IPs."

        # Remove from pending
        self.pending_ops.remove(run_id)

        message = (
            f":white_check_mark: *Apply Complete*\n\n"
            f"*Domain:* {op.domain}\n"
            f"*Change:* `{op.tf_var}`: `{op.old_value}` -> `{op.new_value}`\n"
            f"*Run:* {run_id}\n"
            f"*Status:* applied"
            f"{ip_info}"
        )

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=message,
            data={
                "run_id": run_id,
                "status": "applied",
                "domain": op.domain,
                "tf_var": op.tf_var,
                "old_value": op.old_value,
                "new_value": op.new_value,
            },
        )

    async def _handle_cancel_run(
        self,
        parameters: Dict[str, Any],
        user_id: str,
    ) -> MCPResult:
        run_id = parameters.get("run_id", "").strip()
        if not run_id:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="Missing required parameter: `run_id`",
            )

        op = self.pending_ops.get(run_id)

        # Discard the run in TFC
        user_name = parameters.get("user_name", user_id)
        try:
            await self.tfc_client.discard_run(run_id, comment=f"Cancelled by {user_name} via Slack")
        except Exception as e:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Failed to discard run `{run_id}`: {str(e)}",
            )

        # Remove from pending
        self.pending_ops.remove(run_id)

        domain_info = f" for {op.domain}" if op else ""
        var_info = f" ({op.tf_var}: {op.old_value} -> {op.new_value})" if op else ""

        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=f":no_entry_sign: *Run Cancelled*\n\nRun {run_id}{domain_info}{var_info} has been discarded.",
            data={"run_id": run_id, "status": "discarded"},
        )

    def _handle_show_help(self) -> MCPResult:
        help_text = """*Terraform Manager (/tf)*

Scale OpenStack resources managed by Terraform.

*Scaling:*
- `/tf add <count> <role> to <domain>` — Increase instance count
- `/tf remove <count> <role> from <domain>` — Decrease instance count
- `/tf set <role> <count> in <domain>` — Set exact instance count

*mphpp (region-specific):*
- `/tf add 2 mphpp bos_2 to aptus2` — Specify region
- `/tf add 2 mphpp to aptus2` — Auto-detect if only one region

*View:*
- `/tf show <domain>` — Show current resource counts
- `/tf status` — Show pending operations

*Confirm/Cancel:*
- `/tf confirm <run_id>` — Apply a pending plan
- `/tf cancel <run_id>` — Discard a pending plan

*Supported Roles:*
`mim`, `mimmem`, `mphpp`, `mphhos`, `ts`, `www5`, `ngx`, `ngxint`, `redis`, `mongodb`, `tps`, `harjo`, `provnstatdb5`, `srouter`, `sdecoder`, `scapture`, `sconductor`

*Examples:*
- `/tf add 2 mim to aptus2`
- `/tf remove 1 ts from lionxp`
- `/tf set mim 5 in pubwxp`
- `/tf show aptus2`
"""
        return MCPResult(
            status=MCPResultStatus.SUCCESS,
            message=help_text,
        )

    async def health_check(self) -> bool:
        token = os.environ.get("TFC_TOKEN", "")
        github_token = os.environ.get("TF_GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
        return bool(token) and bool(github_token)
