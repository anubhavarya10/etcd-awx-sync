"""AWX ad-hoc command client for executing commands via AWX API."""

import os
import re
import asyncio
import logging
import ssl
from typing import Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)


class AWXAdHocClient:
    """
    Runs shell commands on remote hosts via AWX ad-hoc commands API.

    Used for Azure hosts (10.253.x.x) where direct SSH from K8s pods
    times out due to network-level issues. AWX can connect fine via
    its ephemeral job pods.
    """

    def __init__(self):
        self.server = os.environ.get("AWX_SERVER", "awx.vivox.com")
        self.username = os.environ.get("AWX_USERNAME", "admin")
        self.password = os.environ.get("AWX_PASSWORD", "")
        self.credential_id = int(os.environ.get("AWX_CREDENTIAL_ID", "5"))
        self.ee_id = int(os.environ.get("AWX_EE_ID", "4"))
        self.base_url = f"https://{self.server}/api/v2"
        self.poll_interval = 2  # seconds between job status polls
        self.job_timeout = 90  # max seconds to wait for job completion (EE pod startup can take 30-40s)

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context that skips certificate verification (self-signed cert)."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _get_auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(self.username, self.password)

    async def _find_inventory_id(
        self, session: aiohttp.ClientSession, inventory_name: str
    ) -> Optional[int]:
        """Look up an AWX inventory ID by name."""
        url = f"{self.base_url}/inventories/"
        params = {"name": inventory_name, "page_size": 1}
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(
                    f"Failed to search inventories: {resp.status}"
                )
                return None
            data = await resp.json()
            results = data.get("results", [])
            if results:
                return results[0]["id"]
        return None

    async def run_command(
        self,
        hostname: str,
        command: str,
        inventory_name: Optional[str] = None,
    ) -> Tuple[str, str, int]:
        """
        Execute a shell command on a host via AWX ad-hoc command.

        Args:
            hostname: The FQDN of the target host (used as limit)
            command: Shell command to execute
            inventory_name: AWX inventory name to use (e.g. "mim-lionamxp")

        Returns:
            Tuple of (stdout, stderr, exit_code)
        """
        ssl_ctx = self._get_ssl_context()
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)

        async with aiohttp.ClientSession(
            auth=self._get_auth(), connector=connector
        ) as session:
            # Find inventory
            inv_id = None
            if inventory_name:
                inv_id = await self._find_inventory_id(session, inventory_name)
                if not inv_id:
                    logger.warning(
                        f"Inventory '{inventory_name}' not found, "
                        f"trying without inventory prefix"
                    )

            if not inv_id:
                # Try the domain portion as inventory name
                if inventory_name and "-" in inventory_name:
                    domain_part = inventory_name.split("-", 1)[1]
                    inv_id = await self._find_inventory_id(session, domain_part)

            if not inv_id:
                return (
                    "",
                    f"Could not find AWX inventory for '{inventory_name}'",
                    -1,
                )

            # Launch ad-hoc command
            adhoc_url = f"{self.base_url}/inventories/{inv_id}/ad_hoc_commands/"
            payload = {
                "module_name": "shell",
                "module_args": command,
                "limit": hostname,
                "credential": self.credential_id,
                "execution_environment": self.ee_id,
            }

            logger.info(
                f"Launching AWX ad-hoc command on {hostname} "
                f"(inventory={inventory_name}, id={inv_id}): {command[:80]}"
            )

            async with session.post(adhoc_url, json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    logger.error(
                        f"AWX ad-hoc launch failed ({resp.status}): {body}"
                    )
                    return ("", f"AWX ad-hoc launch failed: {body[:200]}", -1)
                job_data = await resp.json()

            job_id = job_data["id"]
            job_url = f"{self.base_url}/ad_hoc_commands/{job_id}/"

            # Poll for completion
            status = await self._poll_job_status(session, job_url, job_id)
            if status is None:
                return ("", f"AWX job {job_id} timed out after {self.job_timeout}s", -1)

            # Get job stdout
            stdout_url = f"{job_url}stdout/?format=txt"
            async with session.get(stdout_url) as resp:
                raw_stdout = await resp.text() if resp.status == 200 else ""

            stdout = self._parse_adhoc_output(raw_stdout)

            if status == "successful":
                exit_code = 0
            elif status == "failed":
                exit_code = self._extract_rc(raw_stdout, default=1)
            else:
                exit_code = -1

            stderr = ""
            if status == "error":
                stderr = "AWX job error"
            elif status == "canceled":
                stderr = "AWX job was canceled"

            return stdout, stderr, exit_code

    async def run_command_multi(
        self,
        hostnames: list,
        command: str,
        inventory_name: Optional[str] = None,
    ) -> Dict[str, Tuple[str, str, int]]:
        """
        Execute a shell command on multiple hosts via a single AWX ad-hoc job.

        AWX serializes ad-hoc jobs, so launching one job per host causes
        each to queue behind the previous (~35s each). A single job with
        a comma-separated limit runs on all hosts in parallel via forks.

        Args:
            hostnames: List of FQDNs to target
            command: Shell command to execute
            inventory_name: AWX inventory name (e.g. "mim-lionamxp")

        Returns:
            Dict mapping hostname -> (stdout, stderr, exit_code)
        """
        ssl_ctx = self._get_ssl_context()
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)

        async with aiohttp.ClientSession(
            auth=self._get_auth(), connector=connector
        ) as session:
            # Find inventory
            inv_id = None
            if inventory_name:
                inv_id = await self._find_inventory_id(session, inventory_name)
                if not inv_id and "-" in inventory_name:
                    domain_part = inventory_name.split("-", 1)[1]
                    inv_id = await self._find_inventory_id(session, domain_part)

            if not inv_id:
                error = ("", f"Could not find AWX inventory for '{inventory_name}'", -1)
                return {h: error for h in hostnames}

            # Launch single ad-hoc command targeting all hosts
            limit = ":".join(hostnames)  # AWX uses colon as host separator
            adhoc_url = f"{self.base_url}/inventories/{inv_id}/ad_hoc_commands/"
            payload = {
                "module_name": "shell",
                "module_args": command,
                "limit": limit,
                "credential": self.credential_id,
                "execution_environment": self.ee_id,
                "forks": len(hostnames),
            }

            logger.info(
                f"Launching AWX ad-hoc command on {len(hostnames)} hosts "
                f"(inventory={inventory_name}, id={inv_id}): {command[:80]}"
            )

            async with session.post(adhoc_url, json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    logger.error(f"AWX ad-hoc launch failed ({resp.status}): {body}")
                    error = ("", f"AWX ad-hoc launch failed: {body[:200]}", -1)
                    return {h: error for h in hostnames}
                job_data = await resp.json()

            job_id = job_data["id"]
            job_url = f"{self.base_url}/ad_hoc_commands/{job_id}/"

            # Poll for completion
            job_status = await self._poll_job_status(session, job_url, job_id)
            if job_status is None:
                error = ("", f"AWX job {job_id} timed out after {self.job_timeout}s", -1)
                return {h: error for h in hostnames}

            # Get job stdout and parse per-host results
            stdout_url = f"{job_url}stdout/?format=txt"
            async with session.get(stdout_url) as resp:
                raw_stdout = await resp.text() if resp.status == 200 else ""

            return self._parse_multi_host_output(raw_stdout, hostnames, job_status)

    async def _poll_job_status(
        self,
        session: aiohttp.ClientSession,
        job_url: str,
        job_id: int,
    ) -> Optional[str]:
        """Poll an AWX job until completion. Returns status string or None on timeout."""
        elapsed = 0
        while elapsed < self.job_timeout:
            async with session.get(job_url) as resp:
                if resp.status != 200:
                    return None
                job = await resp.json()

            status = job.get("status", "")
            if status in ("successful", "failed", "error", "canceled"):
                return status

            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval

        return None

    @staticmethod
    def _parse_multi_host_output(
        raw: str,
        hostnames: list,
        job_status: str,
    ) -> Dict[str, Tuple[str, str, int]]:
        """
        Parse AWX ad-hoc output that contains results from multiple hosts.

        Multi-host output format:
            host1 | CHANGED | rc=0 >>
            <output for host1>

            host2 | CHANGED | rc=0 >>
            <output for host2>
        """
        results = {}
        # Split output into per-host blocks using the AWX header pattern
        pattern = re.compile(
            r'^(.+?)\s*\|\s*(CHANGED|FAILED|SUCCESS|UNREACHABLE)\s*\|?\s*(?:rc=(\d+))?\s*>>',
            re.MULTILINE,
        )

        matches = list(pattern.finditer(raw))
        for i, match in enumerate(matches):
            host_from_output = match.group(1).strip()
            status_word = match.group(2)
            rc = int(match.group(3)) if match.group(3) else (0 if status_word in ("CHANGED", "SUCCESS") else 1)

            # Extract the output block between this header and the next
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            output = raw[start:end].strip()

            stderr = ""
            if status_word == "UNREACHABLE":
                stderr = output or "Host unreachable"
                output = ""
                rc = -1

            results[host_from_output] = (output, stderr, rc)

        # Fill in any hosts that didn't appear in the output
        for hostname in hostnames:
            if hostname not in results:
                results[hostname] = ("", "No output from AWX for this host", -1)

        return results

    @staticmethod
    def _parse_adhoc_output(raw: str) -> str:
        """
        Parse AWX ad-hoc command output, stripping the header line.

        AWX ad-hoc output format:
            hostname | CHANGED | rc=0 >>
            <actual command output>
        or:
            hostname | FAILED | rc=1 >>
            <actual command output>
        """
        if not raw:
            return ""

        lines = raw.split("\n")
        # Look for the AWX header pattern in the first few lines
        for i, line in enumerate(lines[:5]):
            if re.match(r'^.+\|\s*(CHANGED|FAILED|SUCCESS)\s*\|\s*rc=\d+\s*>>', line):
                # Return everything after this header line
                return "\n".join(lines[i + 1:]).strip()

        # No header found — return as-is (stripped)
        return raw.strip()

    @staticmethod
    def _extract_rc(raw: str, default: int = 1) -> int:
        """Extract rc=N from AWX ad-hoc output header."""
        match = re.search(r'rc=(\d+)', raw)
        if match:
            return int(match.group(1))
        return default
