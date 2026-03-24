"""Async SSH client for direct server management."""

import os
import asyncio
import logging
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass

import asyncssh

from .awx_client import AWXAdHocClient

logger = logging.getLogger(__name__)


@dataclass
class SSHResult:
    """Result of an SSH command execution."""
    stdout: str
    stderr: str
    exit_code: int
    host: str
    success: bool

    @property
    def output(self) -> str:
        """Combined output (stdout + stderr if present)."""
        if self.stderr and self.exit_code != 0:
            return f"{self.stdout}\n{self.stderr}".strip()
        return self.stdout.strip()


class AsyncSSHClient:
    """
    Async SSH client with support for password-based auth.

    - Regular hosts: SSH as root with password (USERNAME/PASSWORD env vars)
    - Azure hosts (10.253.x.x): SSH as vivoxops with password + sudo
    """

    def __init__(self):
        # Regular server credentials
        self.regular_username = os.environ.get("USERNAME", "root")
        self.regular_password = os.environ.get("PASSWORD")

        # Azure credentials (different user + sudo)
        self.azure_password = os.environ.get("AZURE_SUDO_PASSWORD")

        # AWX client for Azure hosts (direct SSH times out from K8s pods)
        self.awx_client = AWXAdHocClient()

        # Fallback to SSH key if no password configured
        self.ssh_key_path = os.environ.get("SSH_KEY_PATH", "/app/ssh/id_rsa")

        self.connection_timeout = 10
        self.command_timeout = 30

    def _is_azure(self, ip: str) -> bool:
        """Check if IP belongs to Azure (10.253.x.x range)."""
        return ip.startswith("10.253.")

    async def execute(
        self,
        host: str,
        command: str,
        ip: Optional[str] = None,
        timeout: Optional[int] = None,
        inventory_name: Optional[str] = None,
    ) -> SSHResult:
        """
        Execute a command on a remote host.

        Args:
            host: Hostname for logging/display
            command: Command to execute
            ip: IP address to connect to (defaults to host)
            timeout: Command timeout in seconds
            inventory_name: AWX inventory name for Azure hosts (e.g. "mim-lionamxp")

        Returns:
            SSHResult with stdout, stderr, exit_code
        """
        connect_ip = ip or host
        timeout = timeout or self.command_timeout

        try:
            if self._is_azure(connect_ip):
                return await self._execute_azure(host, connect_ip, command, timeout, inventory_name)
            else:
                return await self._execute_regular(host, connect_ip, command, timeout)
        except asyncssh.Error as e:
            logger.error(f"SSH error on {host}: {e}")
            return SSHResult(
                stdout="",
                stderr=str(e),
                exit_code=-1,
                host=host,
                success=False,
            )
        except asyncio.TimeoutError:
            logger.error(f"SSH timeout on {host}")
            return SSHResult(
                stdout="",
                stderr="Connection or command timed out",
                exit_code=-1,
                host=host,
                success=False,
            )
        except Exception as e:
            logger.exception(f"Unexpected error on {host}: {e}")
            return SSHResult(
                stdout="",
                stderr=str(e),
                exit_code=-1,
                host=host,
                success=False,
            )

    async def _execute_regular(
        self,
        host: str,
        ip: str,
        command: str,
        timeout: int,
    ) -> SSHResult:
        """Execute command on regular host (SSH as root with password)."""
        if not self.regular_password:
            return SSHResult(
                stdout="",
                stderr="PASSWORD not configured for regular hosts",
                exit_code=-1,
                host=host,
                success=False,
            )

        logger.debug(f"Connecting to {host} ({ip}) as {self.regular_username} with password")

        async with asyncssh.connect(
            ip,
            username=self.regular_username,
            password=self.regular_password,
            known_hosts=None,  # Disable host key checking for internal servers
            connect_timeout=self.connection_timeout,
        ) as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout,
            )

            return SSHResult(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.exit_status or 0,
                host=host,
                success=result.exit_status == 0,
            )

    async def _execute_azure(
        self,
        host: str,
        ip: str,
        command: str,
        timeout: int,
        inventory_name: Optional[str] = None,
    ) -> SSHResult:
        """Execute command on Azure host via AWX ad-hoc command.

        Direct SSH from K8s pods to Azure hosts (10.253.x.x) times out
        after authentication due to network-level issues. AWX can connect
        fine via its ephemeral job pods, so we route through the AWX
        ad-hoc commands API instead.
        """
        if not self.awx_client.password:
            return SSHResult(
                stdout="",
                stderr="AWX_PASSWORD not configured for Azure host routing",
                exit_code=-1,
                host=host,
                success=False,
            )

        logger.info(f"Routing Azure host {host} ({ip}) through AWX ad-hoc API")

        try:
            stdout, stderr, exit_code = await asyncio.wait_for(
                self.awx_client.run_command(host, command, inventory_name),
                timeout=self.awx_client.job_timeout + 10,  # buffer over AWX's own poll timeout
            )

            return SSHResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                host=host,
                success=exit_code == 0,
            )
        except asyncio.TimeoutError:
            logger.error(f"AWX ad-hoc timeout for {host}")
            return SSHResult(
                stdout="",
                stderr="AWX ad-hoc command timed out",
                exit_code=-1,
                host=host,
                success=False,
            )
        except Exception as e:
            logger.exception(f"AWX ad-hoc error for {host}: {e}")
            return SSHResult(
                stdout="",
                stderr=f"AWX ad-hoc error: {str(e)}",
                exit_code=-1,
                host=host,
                success=False,
            )

    async def execute_azure_batch(
        self,
        hostnames: List[str],
        command: str,
        inventory_name: Optional[str] = None,
    ) -> Dict[str, "SSHResult"]:
        """
        Execute a command on multiple Azure hosts via a single AWX ad-hoc job.

        AWX serializes separate ad-hoc jobs (~35s each). This method batches
        all hosts into one job so they run in parallel via forks.

        Args:
            hostnames: List of host FQDNs
            command: Command to execute
            inventory_name: AWX inventory name (e.g. "mim-lionamxp")

        Returns:
            Dict mapping hostname -> SSHResult
        """
        if not self.awx_client.password:
            return {
                h: SSHResult("", "AWX_PASSWORD not configured", -1, h, False)
                for h in hostnames
            }

        logger.info(
            f"Batch AWX ad-hoc for {len(hostnames)} Azure hosts "
            f"(inventory={inventory_name})"
        )

        try:
            multi_results = await asyncio.wait_for(
                self.awx_client.run_command_multi(hostnames, command, inventory_name),
                timeout=self.awx_client.job_timeout + 10,
            )
        except asyncio.TimeoutError:
            logger.error(f"AWX batch ad-hoc timeout for {len(hostnames)} hosts")
            return {
                h: SSHResult("", "AWX ad-hoc command timed out", -1, h, False)
                for h in hostnames
            }
        except Exception as e:
            logger.exception(f"AWX batch ad-hoc error: {e}")
            return {
                h: SSHResult("", f"AWX ad-hoc error: {e}", -1, h, False)
                for h in hostnames
            }

        results = {}
        for hostname, (stdout, stderr, exit_code) in multi_results.items():
            results[hostname] = SSHResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                host=hostname,
                success=exit_code == 0,
            )
        return results

    async def execute_on_hosts(
        self,
        hosts: List[Dict[str, Any]],
        command: str,
        max_concurrent: int = 10,
    ) -> List[SSHResult]:
        """
        Execute command on multiple hosts concurrently.

        Args:
            hosts: List of host dicts with 'hostname' and 'ip' keys
            command: Command to execute on all hosts
            max_concurrent: Maximum concurrent connections

        Returns:
            List of SSHResult for each host
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def execute_with_semaphore(host_info):
            async with semaphore:
                hostname = host_info.get("hostname", host_info.get("host", "unknown"))
                ip = host_info.get("ip", host_info.get("private_ip", hostname))
                return await self.execute(hostname, command, ip)

        tasks = [execute_with_semaphore(h) for h in hosts]
        return await asyncio.gather(*tasks)

    async def check_service_status(
        self,
        host: str,
        service: str,
        ip: Optional[str] = None,
        inventory_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Check the status of a systemd service.

        Returns dict with:
            - host: hostname
            - service: service name
            - status: 'active', 'inactive', 'failed', 'unknown'
            - active_state: full active state
            - sub_state: sub state (running, dead, etc)
            - since: when the service entered current state
            - logs: last 10 lines if failed
        """
        # Get service status
        status_cmd = f"systemctl show {service} --property=ActiveState,SubState,ActiveEnterTimestamp --no-pager"
        result = await self.execute(host, status_cmd, ip, inventory_name=inventory_name)

        status_info = {
            "host": host,
            "service": service,
            "status": "unknown",
            "active_state": "",
            "sub_state": "",
            "since": "",
            "logs": "",
        }

        # Check if we have valid output (systemctl may return non-zero but still have output)
        if not result.stdout and not result.success:
            status_info["status"] = "error"
            status_info["error"] = result.stderr or "SSH connection failed"
            return status_info

        # Parse systemctl output
        for line in result.stdout.split("\n"):
            if "=" in line:
                key, value = line.split("=", 1)
                if key == "ActiveState":
                    status_info["active_state"] = value
                    status_info["status"] = value
                elif key == "SubState":
                    status_info["sub_state"] = value
                elif key == "ActiveEnterTimestamp":
                    status_info["since"] = value

        # If failed, get last 10 lines of logs
        if status_info["status"] == "failed":
            logs_cmd = f"journalctl -u {service} -n 10 --no-pager"
            logs_result = await self.execute(host, logs_cmd, ip, inventory_name=inventory_name)
            if logs_result.success:
                status_info["logs"] = logs_result.stdout

        return status_info

    async def manage_service(
        self,
        host: str,
        service: str,
        action: str,
        ip: Optional[str] = None,
        inventory_name: Optional[str] = None,
    ) -> SSHResult:
        """
        Start, stop, or restart a service.

        Args:
            host: Hostname
            service: Service name
            action: 'start', 'stop', 'restart', 'status'
            ip: IP address

        Returns:
            SSHResult with command output
        """
        if action not in ("start", "stop", "restart", "status"):
            return SSHResult(
                stdout="",
                stderr=f"Invalid action: {action}",
                exit_code=-1,
                host=host,
                success=False,
            )

        command = f"systemctl {action} {service}"
        return await self.execute(host, command, ip, inventory_name=inventory_name)

    # Mapping of services to their log file paths
    SERVICE_LOG_FILES = {
        "mongooseim": "/var/log/mongooseim/mongooseim.log",
        "morpheus": "/var/log/morpheus/morpheus.log",
        "nginx": "/var/log/nginx/error.log",
        "redis": "/var/log/redis/redis.log",
        "mongod": "/var/log/mongodb/mongod.log",
    }

    async def get_service_logs(
        self,
        host: str,
        service: str,
        lines: int = 50,
        ip: Optional[str] = None,
        inventory_name: Optional[str] = None,
    ) -> SSHResult:
        """
        Get logs for a service - tries log file first, then journalctl.

        Args:
            host: Hostname
            service: Service name
            lines: Number of lines to fetch
            ip: IP address

        Returns:
            SSHResult with log output
        """
        # Try log file first if we know the path
        log_file = self.SERVICE_LOG_FILES.get(service)
        if log_file:
            # Check if log file exists and read from it
            command = f"test -f {log_file} && tail -n {lines} {log_file} || journalctl -u {service} -n {lines} --no-pager"
        else:
            # Fall back to journalctl
            command = f"journalctl -u {service} -n {lines} --no-pager"

        return await self.execute(host, command, ip, inventory_name=inventory_name)
