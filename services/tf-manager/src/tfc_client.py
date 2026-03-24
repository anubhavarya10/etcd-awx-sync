"""Terraform Cloud API v2 client."""

import os
import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TFC_BASE_URL = "https://app.terraform.io/api/v2"
POLL_INTERVAL = 5  # seconds
MAX_POLL_TIME = 600  # 10 minutes


class TFCClient:
    """Client for Terraform Cloud API v2."""

    def __init__(self):
        self.org = os.environ.get("TFC_ORG", "unity-technologies")
        token = os.environ.get("TFC_TOKEN", "")
        if not token:
            raise ValueError("TFC_TOKEN environment variable is not set")

        self._client = httpx.AsyncClient(
            base_url=TFC_BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/vnd.api+json",
            },
            timeout=30,
        )

    async def get_workspace(self, domain: str) -> Dict[str, Any]:
        """
        Get workspace details by domain name.

        Workspace naming convention: vivox-ops-openstack-<domain>
        """
        workspace_name = f"vivox-ops-openstack-{domain}"
        response = await self._client.get(
            f"/organizations/{self.org}/workspaces/{workspace_name}"
        )
        response.raise_for_status()
        return response.json()["data"]

    async def create_run(self, workspace_id: str, message: str) -> Dict[str, Any]:
        """
        Create a new run (plan) in a workspace.

        auto-apply is set to false — requires explicit confirmation.
        """
        payload = {
            "data": {
                "type": "runs",
                "attributes": {
                    "message": message,
                    "auto-apply": False,
                },
                "relationships": {
                    "workspace": {
                        "data": {
                            "type": "workspaces",
                            "id": workspace_id,
                        }
                    }
                },
            }
        }

        response = await self._client.post("/runs", json=payload)
        response.raise_for_status()
        return response.json()["data"]

    async def list_runs(self, workspace_id: str, page_size: int = 5) -> List[Dict[str, Any]]:
        """List recent runs for a workspace, ordered by creation time (newest first)."""
        response = await self._client.get(
            f"/workspaces/{workspace_id}/runs",
            params={"page[size]": page_size},
        )
        response.raise_for_status()
        return response.json()["data"]

    async def find_vcs_run(self, workspace_id: str, commit_message: str, max_wait: int = 60) -> Dict[str, Any]:
        """
        Wait for a VCS-triggered run to appear for our commit.

        After pushing to GitHub, the VCS webhook fires and TFC creates a run
        with source='tfe-configuration-version'. We match by the git commit
        message which TFC uses as the run message.

        Returns the run data.
        Raises TimeoutError if no matching run appears within max_wait seconds.
        """
        elapsed = 0
        while elapsed < max_wait:
            runs = await self.list_runs(workspace_id)
            for run in runs:
                attrs = run.get("attributes", {})
                source = attrs.get("source", "")
                status = attrs.get("status", "")
                message = attrs.get("message", "")

                # Match VCS-triggered runs by our commit message
                if source == "tfe-configuration-version" and commit_message in message:
                    # Skip terminal states (old runs with same message)
                    if status not in ("discarded", "canceled", "force_canceled", "errored", "applied"):
                        logger.info(f"Found VCS-triggered run: {run['id']} (status={status})")
                        return run

            logger.info(f"Waiting for VCS-triggered run (elapsed: {elapsed}s)...")
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        raise TimeoutError(
            f"No VCS-triggered run appeared for commit '{commit_message}' "
            f"within {max_wait}s. Check GitHub webhook delivery."
        )

    async def get_run(self, run_id: str) -> Dict[str, Any]:
        """Get run details."""
        response = await self._client.get(f"/runs/{run_id}")
        response.raise_for_status()
        return response.json()["data"]

    async def get_plan(self, plan_id: str) -> Dict[str, Any]:
        """Get plan details including resource counts."""
        response = await self._client.get(f"/plans/{plan_id}")
        response.raise_for_status()
        return response.json()["data"]

    async def apply_run(self, run_id: str, comment: str = "") -> None:
        """Apply (confirm) a planned run."""
        payload = {"comment": comment} if comment else {}
        response = await self._client.post(
            f"/runs/{run_id}/actions/apply",
            json=payload,
        )
        response.raise_for_status()
        logger.info(f"Applied run {run_id}")

    async def discard_run(self, run_id: str, comment: str = "") -> None:
        """Discard (cancel) a planned run."""
        payload = {"comment": comment} if comment else {}
        response = await self._client.post(
            f"/runs/{run_id}/actions/discard",
            json=payload,
        )
        response.raise_for_status()
        logger.info(f"Discarded run {run_id}")

    async def wait_for_plan(self, run_id: str) -> Dict[str, Any]:
        """
        Poll a run until the plan completes.

        Returns the run data when status is 'planned', 'planned_and_finished',
        or an error/terminal state.
        """
        elapsed = 0
        while elapsed < MAX_POLL_TIME:
            run = await self.get_run(run_id)
            status = run["attributes"]["status"]

            logger.info(f"Run {run_id} status: {status} (elapsed: {elapsed}s)")

            if status in ("planned", "planned_and_finished", "cost_estimated", "policy_checked", "policy_soft_failed"):
                return run
            elif status in ("errored", "canceled", "discarded", "force_canceled", "policy_override"):
                return run
            elif status in ("planning", "pending", "plan_queued", "cost_estimating", "policy_checking"):
                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
            else:
                # Unknown status, keep polling
                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

        raise TimeoutError(f"Plan for run {run_id} did not complete within {MAX_POLL_TIME}s")

    async def wait_for_apply(self, run_id: str) -> Dict[str, Any]:
        """
        Poll a run until the apply completes.

        Returns the run data when status is 'applied' or a terminal state.
        """
        elapsed = 0
        while elapsed < MAX_POLL_TIME:
            run = await self.get_run(run_id)
            status = run["attributes"]["status"]

            logger.info(f"Run {run_id} apply status: {status} (elapsed: {elapsed}s)")

            if status == "applied":
                return run
            elif status in ("errored", "canceled", "discarded", "force_canceled"):
                return run
            elif status in ("applying", "confirmed"):
                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
            else:
                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

        raise TimeoutError(f"Apply for run {run_id} did not complete within {MAX_POLL_TIME}s")

    async def get_state_outputs(self, workspace_id: str) -> Dict[str, Any]:
        """
        Get the current state version outputs for a workspace.

        Returns outputs dict (e.g., IP addresses of created resources).
        """
        response = await self._client.get(
            f"/workspaces/{workspace_id}/current-state-version",
            params={"include": "outputs"},
        )
        response.raise_for_status()

        data = response.json()
        outputs = {}

        included = data.get("included", [])
        for item in included:
            if item["type"] == "state-version-outputs":
                name = item["attributes"]["name"]
                value = item["attributes"]["value"]
                outputs[name] = value

        return outputs

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()
