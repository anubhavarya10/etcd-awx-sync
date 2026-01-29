"""MCP Registry for managing multiple MCP implementations."""

from typing import Dict, List, Optional, Type
from .base import BaseMCP, MCPResult, MCPResultStatus
import logging

logger = logging.getLogger(__name__)


class MCPRegistry:
    """
    Registry for managing multiple MCP implementations.

    The registry:
    1. Stores all registered MCPs
    2. Provides combined LLM context for intent routing
    3. Routes actions to appropriate MCPs
    4. Handles confirmation callbacks
    """

    def __init__(self):
        self._mcps: Dict[str, BaseMCP] = {}

    def register(self, mcp: BaseMCP) -> None:
        """Register an MCP instance."""
        if mcp.name in self._mcps:
            logger.warning(f"MCP '{mcp.name}' is already registered, replacing...")
        self._mcps[mcp.name] = mcp
        logger.info(f"Registered MCP: {mcp.name} ({len(mcp.get_actions())} actions)")

    def unregister(self, name: str) -> None:
        """Unregister an MCP by name."""
        if name in self._mcps:
            del self._mcps[name]
            logger.info(f"Unregistered MCP: {name}")

    def get(self, name: str) -> Optional[BaseMCP]:
        """Get an MCP by name."""
        return self._mcps.get(name)

    def list_mcps(self) -> List[BaseMCP]:
        """List all registered MCPs."""
        return list(self._mcps.values())

    def get_llm_context(self) -> str:
        """
        Get combined LLM context for all registered MCPs.
        Used by the agent to help LLM understand all available capabilities.
        """
        if not self._mcps:
            return "No MCPs are currently registered."

        contexts = []
        for mcp in self._mcps.values():
            contexts.append(mcp.get_llm_context())

        return "\n---\n".join(contexts)

    def get_mcp_list_for_llm(self) -> str:
        """Get a simple list of MCPs for LLM."""
        if not self._mcps:
            return "No MCPs available."

        lines = ["Available MCPs:"]
        for mcp in self._mcps.values():
            lines.append(f"- {mcp.name}: {mcp.description}")
        return "\n".join(lines)

    async def route_action(
        self,
        mcp_name: str,
        action: str,
        parameters: Dict,
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """
        Route an action to the appropriate MCP.

        Args:
            mcp_name: Name of the target MCP
            action: Action to execute
            parameters: Action parameters
            user_id: Slack user ID
            channel_id: Slack channel ID

        Returns:
            MCPResult from the MCP
        """
        mcp = self._mcps.get(mcp_name)
        if not mcp:
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Unknown MCP: {mcp_name}. Available MCPs: {', '.join(self._mcps.keys())}"
            )

        action_def = mcp.get_action(action)
        if not action_def:
            available = [a.name for a in mcp.get_actions()]
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Unknown action '{action}' for MCP '{mcp_name}'. Available: {', '.join(available)}"
            )

        try:
            return await mcp.execute(action, parameters, user_id, channel_id)
        except Exception as e:
            logger.exception(f"Error executing {mcp_name}.{action}")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message=f"Error executing action: {str(e)}"
            )

    async def handle_confirmation(
        self,
        action_id: str,
        confirmed: bool,
        user_id: str,
    ) -> MCPResult:
        """
        Handle a confirmation callback by finding the MCP that owns it.

        Args:
            action_id: The action ID from the confirmation
            confirmed: Whether user confirmed
            user_id: User who responded

        Returns:
            MCPResult from the MCP
        """
        # Find which MCP has this pending confirmation
        for mcp in self._mcps.values():
            if action_id in mcp._pending_confirmations:
                return await mcp.handle_confirmation(action_id, confirmed, user_id)

        return MCPResult(
            status=MCPResultStatus.ERROR,
            message="This action has expired or was already processed."
        )

    async def health_check(self) -> Dict[str, bool]:
        """
        Check health of all registered MCPs.

        Returns:
            Dict mapping MCP names to their health status
        """
        results = {}
        for name, mcp in self._mcps.items():
            try:
                results[name] = await mcp.health_check()
            except Exception as e:
                logger.error(f"Health check failed for {name}: {e}")
                results[name] = False
        return results


# Global registry instance
_registry: Optional[MCPRegistry] = None


def get_registry() -> MCPRegistry:
    """Get the global MCP registry instance."""
    global _registry
    if _registry is None:
        _registry = MCPRegistry()
    return _registry


def register_mcp(mcp: BaseMCP) -> None:
    """Convenience function to register an MCP with the global registry."""
    get_registry().register(mcp)
