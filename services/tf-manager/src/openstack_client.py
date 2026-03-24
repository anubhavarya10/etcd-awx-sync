"""OpenStack server query via SSH through jump host.

SSH chain: tf-manager pod -> jmp1 (10.0.25.10) -> ostack2-osa
On ostack2-osa: runs `openstack server list` with pre-configured credentials.
"""

import os
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import asyncssh

logger = logging.getLogger(__name__)

JUMP_HOST = os.environ.get("OPENSTACK_JUMP_HOST", "10.0.25.10")
OPENSTACK_HOST_ALIAS = os.environ.get("OPENSTACK_HOST", "ostack2-osa")
SSH_USERNAME = os.environ.get("SSH_USERNAME", "root")


class OpenStackClient:
    """Query OpenStack server list via SSH through jump host."""

    def __init__(self):
        self.jump_host = JUMP_HOST
        self.ostack_host = OPENSTACK_HOST_ALIAS
        self.username = SSH_USERNAME
        self.password = os.environ.get("SSH_PASSWORD", "")

    async def get_servers(self, role: str, domain: str) -> List[Dict[str, Any]]:
        """
        Get OpenStack servers matching a role and domain.

        SSHes through jmp1 to the OpenStack controller and runs:
          openstack server list --all --name <role>-<domain> -f json

        Returns list of server dicts with keys: ID, Name, Status, Networks.
        """
        if not self.password:
            logger.warning("SSH_PASSWORD not set, cannot query OpenStack")
            return []

        name_filter = f"{role}-{domain}"
        remote_cmd = (
            f"ssh -o StrictHostKeyChecking=no {self.ostack_host} "
            f"'source /opt/venv-openstack/bin/activate && "
            f"source /etc/kolla/admin-openrc.sh && "
            f"openstack server list --all --name {name_filter} -f json'"
        )

        try:
            async with asyncssh.connect(
                self.jump_host,
                username=self.username,
                password=self.password,
                known_hosts=None,
                connect_timeout=10,
            ) as conn:
                logger.info(f"SSH to {self.jump_host}, running: openstack server list --name {name_filter}")
                result = await asyncio.wait_for(conn.run(remote_cmd), timeout=45)

                if result.exit_status != 0:
                    logger.error(f"OpenStack query failed: {result.stderr}")
                    return []

                stdout = result.stdout.strip()
                if not stdout:
                    return []

                servers = json.loads(stdout)
                return servers

        except asyncssh.Error as e:
            logger.error(f"SSH error querying OpenStack: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OpenStack JSON output: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error querying OpenStack: {e}")
            return []

    def parse_server_ips(self, servers: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """
        Extract server names and IPs from OpenStack server list output.

        Returns list of dicts with 'name', 'status', and 'ips'.
        """
        results = []
        for server in servers:
            name = server.get("Name", server.get("name", "unknown"))
            status = server.get("Status", server.get("status", "unknown"))
            networks = server.get("Networks", server.get("networks", ""))

            # Parse IPs from networks string like "net_odmz_vlan_23=10.0.65.96; net_pub_vlan_102=85.236.102.92"
            ips = []
            if isinstance(networks, str):
                for part in networks.split(";"):
                    part = part.strip()
                    if "=" in part:
                        ip = part.split("=", 1)[1].strip()
                        ips.append(ip)
            elif isinstance(networks, dict):
                for net_name, ip_list in networks.items():
                    if isinstance(ip_list, list):
                        ips.extend(ip_list)
                    elif isinstance(ip_list, str):
                        ips.append(ip_list)

            results.append({
                "name": name,
                "status": status,
                "ips": ips,
            })

        return results
