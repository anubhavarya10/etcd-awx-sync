"""Base MCP class that all MCPs inherit from."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Callable
import asyncio
import logging

logger = logging.getLogger(__name__)


class MCPResultStatus(Enum):
    """Status of an MCP operation result."""
    SUCCESS = "success"
    ERROR = "error"
    PENDING = "pending"
    CANCELLED = "cancelled"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass
class MCPAction:
    """
    Represents an action that an MCP can perform.

    Attributes:
        name: Unique action identifier (e.g., 'sync', 'list-domains')
        description: Human-readable description for LLM context
        parameters: List of parameter definitions
        requires_confirmation: Whether user must confirm before execution
        examples: Example prompts that trigger this action
    """
    name: str
    description: str
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    requires_confirmation: bool = True
    examples: List[str] = field(default_factory=list)

    def to_llm_context(self) -> str:
        """Convert action to LLM-readable context."""
        params_str = ""
        if self.parameters:
            params_list = []
            for p in self.parameters:
                param_desc = f"  - {p['name']}"
                if p.get('required'):
                    param_desc += " (required)"
                else:
                    param_desc += " (optional)"
                if p.get('description'):
                    param_desc += f": {p['description']}"
                params_list.append(param_desc)
            params_str = "\n" + "\n".join(params_list)

        examples_str = ""
        if self.examples:
            examples_str = "\n  Examples: " + ", ".join(f'"{e}"' for e in self.examples[:3])

        return f"- {self.name}: {self.description}{params_str}{examples_str}"


@dataclass
class MCPResult:
    """
    Result of an MCP operation.

    Attributes:
        status: Operation status
        message: Human-readable message
        data: Operation result data
        confirmation_prompt: If needs_confirmation, the prompt to show user
        action_id: Unique ID for tracking confirmations
    """
    status: MCPResultStatus
    message: str
    data: Optional[Dict[str, Any]] = None
    confirmation_prompt: Optional[str] = None
    action_id: Optional[str] = None
    blocks: Optional[List[Dict[str, Any]]] = None  # Slack Block Kit blocks

    def to_slack_message(self) -> Dict[str, Any]:
        """Convert result to Slack message format."""
        result = {"text": self.message}
        if self.blocks:
            result["blocks"] = self.blocks
        return result


class BaseMCP(ABC):
    """
    Base class for all MCP implementations.

    MCPs (Model Context Protocols) are modular handlers that:
    1. Define available actions and their parameters
    2. Process user intents routed by the agent
    3. Execute operations and return results
    4. Support confirmation workflows for destructive actions
    """

    def __init__(self):
        self._actions: Dict[str, MCPAction] = {}
        self._pending_confirmations: Dict[str, Dict[str, Any]] = {}
        self._setup_actions()

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this MCP."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this MCP does."""
        pass

    @property
    def display_name(self) -> str:
        """Display name for UI purposes."""
        return self.name.replace("-", " ").replace("_", " ").title()

    @abstractmethod
    def _setup_actions(self) -> None:
        """Register all available actions. Called during __init__."""
        pass

    def register_action(self, action: MCPAction) -> None:
        """Register an action with this MCP."""
        self._actions[action.name] = action

    def get_actions(self) -> List[MCPAction]:
        """Get all registered actions."""
        return list(self._actions.values())

    def get_action(self, name: str) -> Optional[MCPAction]:
        """Get a specific action by name."""
        return self._actions.get(name)

    def get_llm_context(self) -> str:
        """
        Get context string for the LLM describing this MCP and its actions.
        Used by the agent to help LLM understand available capabilities.
        """
        actions_context = "\n".join(a.to_llm_context() for a in self._actions.values())
        return f"""
MCP: {self.name}
Description: {self.description}

Available Actions:
{actions_context}
"""

    @abstractmethod
    async def execute(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """
        Execute an action with the given parameters.

        Args:
            action: Action name to execute
            parameters: Action parameters extracted by LLM
            user_id: Slack user ID making the request
            channel_id: Slack channel ID where request was made

        Returns:
            MCPResult with operation outcome
        """
        pass

    async def handle_confirmation(
        self,
        action_id: str,
        confirmed: bool,
        user_id: str,
    ) -> MCPResult:
        """
        Handle user confirmation for a pending action.

        Args:
            action_id: The action ID from MCPResult.action_id
            confirmed: Whether user confirmed (True) or cancelled (False)
            user_id: Slack user ID who responded

        Returns:
            MCPResult with final operation outcome
        """
        logger.info(f"Looking for action_id={action_id} in pending={list(self._pending_confirmations.keys())}")

        if action_id not in self._pending_confirmations:
            logger.warning(f"action_id={action_id} NOT FOUND in pending confirmations")
            return MCPResult(
                status=MCPResultStatus.ERROR,
                message="This action has expired or was already processed."
            )

        pending = self._pending_confirmations.pop(action_id)
        logger.info(f"Found pending action: {pending['action']} with params {pending['parameters']}")

        if not confirmed:
            return MCPResult(
                status=MCPResultStatus.CANCELLED,
                message="Action cancelled."
            )

        # Execute the pending action
        return await self._execute_confirmed(
            action=pending["action"],
            parameters=pending["parameters"],
            user_id=user_id,
            channel_id=pending["channel_id"],
        )

    async def _execute_confirmed(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
    ) -> MCPResult:
        """
        Execute an action after confirmation.
        Override in subclass if post-confirmation logic differs.
        """
        return await self.execute(action, parameters, user_id, channel_id)

    def create_confirmation(
        self,
        action: str,
        parameters: Dict[str, Any],
        user_id: str,
        channel_id: str,
        confirmation_message: str,
    ) -> MCPResult:
        """
        Create a confirmation prompt for a pending action.

        Args:
            action: Action name
            parameters: Action parameters
            user_id: Requesting user ID
            channel_id: Channel ID
            confirmation_message: Message to show user

        Returns:
            MCPResult with NEEDS_CONFIRMATION status
        """
        import uuid
        action_id = str(uuid.uuid4())

        self._pending_confirmations[action_id] = {
            "action": action,
            "parameters": parameters,
            "user_id": user_id,
            "channel_id": channel_id,
        }
        logger.info(f"Created confirmation: action_id={action_id}, action={action}, params={parameters}")

        # Create Slack Block Kit confirmation buttons
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": confirmation_message
                }
            },
            {
                "type": "actions",
                "block_id": f"confirm_{action_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Confirm"},
                        "style": "primary",
                        "action_id": f"confirm_{action_id}",
                        "value": action_id
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": f"cancel_{action_id}",
                        "value": action_id
                    }
                ]
            }
        ]

        return MCPResult(
            status=MCPResultStatus.NEEDS_CONFIRMATION,
            message=confirmation_message,
            action_id=action_id,
            blocks=blocks,
        )

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if this MCP's dependencies are healthy.
        Used for K8s readiness probes.
        """
        pass
