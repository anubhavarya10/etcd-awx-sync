"""Remote MCP wrapper for calling MCP services over HTTP."""

import os
import logging
from typing import Any, Dict, List, Optional

import httpx

from .base import BaseMCP, MCPAction, MCPResult, MCPResultStatus

logger = logging.getLogger(__name__)


class RemoteMCP(BaseMCP):
    """
    Wrapper for remote MCP services accessed via HTTP.

    This allows the Slack bot to call MCPs running in separate pods/services.
    """

    def __init__(
        self,
        name: str,
        url: str,
        description: str = "",
        timeout: int = 60,
    ):
        """
        Initialize a remote MCP.

        Args:
            name: MCP name (must match the remote service's name)
            url: Base URL of the remote MCP service (e.g., http://service-manager:8081)
            description: Description of the MCP
            timeout: HTTP request timeout in seconds
        """
        self._name = name
        self._url = url.rstrip("/")
        self._description = description or f"Remote MCP: {name}"
        self._timeout = timeout
        self._cached_actions: List[MCPAction] = []
        self._http_client = httpx.AsyncClient(timeout=timeout)

        # Don't call super().__init__() yet - we'll fetch actions first
        self._actions: Dict[str, MCPAction] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    def _setup_actions(self) -> None:
        """Actions are fetched from remote service, not set up locally."""
        pass

    async def fetch_actions(self) -> bool:
        """
        Fetch available actions from the remote MCP service.

        Returns True if successful.
        """
        try:
            response = await self._http_client.get(f"{self._url}/info")
            response.raise_for_status()

            data = response.json()
            self._description = data.get("description", self._description)

            # Parse actions
            for action_data in data.get("actions", []):
                action = MCPAction(
                    name=action_data.get("name", ""),
                    description=action_data.get("description", ""),
                    parameters=action_data.get("parameters", []),
                    requires_confirmation=action_data.get("requires_confirmation", False),
                    examples=action_data.get("examples", []),
                )
                self._actions[action.name] = action
                self._cached_actions.append(action)

            logger.info(f"Fetched {len(self._actions)} actions from remote MCP {self._name}")
            return True

        except Exception as e:
            logger.error(f"Failed to fetch actions from {self._name} at {self._url}: {e}")
            return False

    def get_actions(self) -> List[MCPAction]:
        """Get all registered actions."""
        return list(self._actions.values())

    def get_action(self, name: str) -> Optional[MCPAction]:
        """Get a specific action by name."""
        return self._actions.get(name)

    async def execute(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """
        Execute an action on the remote MCP service.

        This makes an HTTP POST to the remote service's /execute endpoint.
        """
        try:
            response = await self._http_client.post(
                f"{self._url}/execute",
                json={
                    "action": action,
                    "parameters": parameters,
                    "user_id": user_id,
                    "channel_id": channel_id,
                },
            )

            data = response.json()

            # Parse status
            status_str = data.get("status", "error")
            try:
                status = MCPResultStatus(status_str)
            except ValueError:
                status = MCPResultStatus.ERROR

            return MCPResult(
                status=status,
                message=data.get("message", ""),
                data=data.get("data"),
            )

        except httpx.TimeoutException:
            logger.error(f"Timeout calling {self._name} at {self._url}")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Timeout calling remote service {self._name}",
            )
        except Exception as e:
            logger.exception(f"Error calling {self._name}: {e}")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Error calling remote service: {str(e)}",
            )

    async def health_check(self) -> bool:
        """Check if the remote MCP service is healthy."""
        try:
            response = await self._http_client.get(
                f"{self._url}/ready",
                timeout=5,
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Health check failed for {self._name}: {e}")
            return False

    async def close(self):
        """Close the HTTP client."""
        await self._http_client.aclose()


async def create_remote_mcp(name: str, url: str, description: str = "", timeout: int = 60) -> Optional[RemoteMCP]:
    """
    Create and initialize a remote MCP.

    This fetches the available actions from the remote service.

    Args:
        name: MCP name
        url: Base URL of the remote service
        description: Optional description
        timeout: HTTP request timeout in seconds

    Returns:
        RemoteMCP instance if successful, None if failed
    """
    mcp = RemoteMCP(name=name, url=url, description=description, timeout=timeout)

    if await mcp.fetch_actions():
        return mcp
    else:
        await mcp.close()
        return None
