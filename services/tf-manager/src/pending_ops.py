"""In-memory store for pending Terraform Cloud operations."""

import time
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

# 30-minute expiry for pending operations
PENDING_EXPIRY_SECONDS = 1800


@dataclass
class PendingOp:
    """A pending Terraform Cloud operation awaiting confirmation."""
    run_id: str
    workspace_id: str
    workspace_name: str
    domain: str
    role: str
    tf_var: str
    old_value: int
    new_value: int
    plan_summary: str
    user_id: str
    channel_id: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > PENDING_EXPIRY_SECONDS


class PendingOpsStore:
    """In-memory store for pending TFC operations."""

    def __init__(self):
        self._ops: Dict[str, PendingOp] = {}

    def add(self, op: PendingOp) -> None:
        """Add a pending operation."""
        self._cleanup_expired()
        self._ops[op.run_id] = op
        logger.info(f"Stored pending op: run_id={op.run_id}, {op.tf_var}: {op.old_value} -> {op.new_value}")

    def get(self, run_id: str) -> Optional[PendingOp]:
        """Get a pending operation by run ID."""
        self._cleanup_expired()
        return self._ops.get(run_id)

    def remove(self, run_id: str) -> Optional[PendingOp]:
        """Remove and return a pending operation."""
        return self._ops.pop(run_id, None)

    def list_all(self) -> List[PendingOp]:
        """List all non-expired pending operations."""
        self._cleanup_expired()
        return list(self._ops.values())

    def _cleanup_expired(self) -> None:
        """Remove expired operations."""
        expired = [rid for rid, op in self._ops.items() if op.is_expired()]
        for rid in expired:
            logger.info(f"Expired pending op: run_id={rid}")
            del self._ops[rid]
