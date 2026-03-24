"""Async SSH client for direct server management."""

import os
import asyncio
import logging
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass

import asyncssh

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
    Async SSH client with support for both key-based and password-based auth.

    - Regular hosts: SSH as root with key from /app/ssh/id_rsa
    - Azure hosts (10.253.x.x): SSH as vivoxops with password + sudo
    """

    def __init__(self):
        self.ssh_key_path = os.environ.get("SSH_KEY_PATH", "/app/ssh/id_rsa")
        self.azure_password = os.environ.get("AZURE_SUDO_PASSWORD")
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
    ) -> SSHResult:
        """
        Execute a command on a remote host.

        Args:
            host: Hostname for logging/display
            command: Command to execute
            ip: IP address to connect to (defaults to host)
            timeout: Command timeout in seconds

        Returns:
            SSHResult with stdout, stderr, exit_code
        """
        connect_ip = ip or host
        timeout = timeout or self.command_timeout

        try:
            if self._is_azure(connect_ip):
                return await self._execute_azure(host, connect_ip, command, timeout)
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
        """Execute command on regular host (SSH as root with key)."""
        logger.debug(f"Connecting to {host} ({ip}) as root with key")

        async with asyncssh.connect(
            ip,
            username="root",
            client_keys=[self.ssh_key_path],
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
    ) -> SSHResult:
        """Execute command on Azure host (SSH as vivoxops with sudo)."""
        if not self.azure_password:
            return SSHResult(
                stdout="",
                stderr="AZURE_SUDO_PASSWORD not configured",
                exit_code=-1,
                host=host,
                success=False,
            )

        logger.debug(f"Connecting to {host} ({ip}) as vivoxops with password")

        # Wrap command with sudo
        # Using sudo -S to read password from stdin
        sudo_command = f"echo '{self.azure_password}' | sudo -S {command}"

        async with asyncssh.connect(
            ip,
            username="vivoxops",
            password=self.azure_password,
            known_hosts=None,
            connect_timeout=self.connection_timeout,
        ) as conn:
            result = await asyncio.wait_for(
                conn.run(sudo_command, check=False),
                timeout=timeout,
            )

            # Filter out the sudo password prompt from stderr
            stderr = result.stderr or ""
            stderr_lines = [
                line for line in stderr.split("\n")
                if not line.startswith("[sudo]") and "password" not in line.lower()
            ]

            return SSHResult(
                stdout=result.stdout or "",
                stderr="\n".join(stderr_lines),
                exit_code=result.exit_status or 0,
                host=host,
                success=result.exit_status == 0,
            )

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
        status_cmd = f"systemctl show {service} --property=ActiveState,SubState,StateChangeTimestamp --no-pager"
        result = await self.execute(host, status_cmd, ip)

        status_info = {
            "host": host,
            "service": service,
            "status": "unknown",
            "active_state": "",
            "sub_state": "",
            "since": "",
            "logs": "",
        }

        if not result.success:
            status_info["status"] = "error"
            status_info["error"] = result.stderr
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
                elif key == "StateChangeTimestamp":
                    status_info["since"] = value

        # If failed, get last 10 lines of logs
        if status_info["status"] == "failed":
            logs_cmd = f"journalctl -u {service} -n 10 --no-pager"
            logs_result = await self.execute(host, logs_cmd, ip)
            if logs_result.success:
                status_info["logs"] = logs_result.stdout

        return status_info

    async def manage_service(
        self,
        host: str,
        service: str,
        action: str,
        ip: Optional[str] = None,
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
        return await self.execute(host, command, ip)

    async def get_service_logs(
        self,
        host: str,
        service: str,
        lines: int = 50,
        ip: Optional[str] = None,
    ) -> SSHResult:
        """
        Get journalctl logs for a service.

        Args:
            host: Hostname
            service: Service name
            lines: Number of lines to fetch
            ip: IP address

        Returns:
            SSHResult with log output
        """
        command = f"journalctl -u {service} -n {lines} --no-pager"
        return await self.execute(host, command, ip)
