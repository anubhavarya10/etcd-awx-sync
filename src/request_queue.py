"""
Request Queue System for handling multiple user requests.

Features:
- Queue system with FIFO ordering
- Parallel execution (configurable max concurrent)
- Deduplication (prevent same playbook on same inventory)
- Priority handling (high/normal/low)
- User tracking and notifications
"""

import asyncio
import heapq
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class RequestPriority(Enum):
    """Priority levels for requests."""
    HIGH = 1      # Emergency/critical - processed first
    NORMAL = 2    # Standard requests
    LOW = 3       # Background/batch jobs


class RequestStatus(Enum):
    """Status of a request."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"


@dataclass(order=True)
class PlaybookRequest:
    """A request to run a playbook."""
    # For priority queue ordering
    sort_key: Tuple[int, float] = field(compare=True, repr=False)

    # Request details (not used for comparison)
    id: str = field(compare=False)
    user_id: str = field(compare=False)
    user_name: str = field(compare=False)
    channel_id: str = field(compare=False)
    playbook: str = field(compare=False)
    inventory: str = field(compare=False)
    extra_vars: Dict[str, Any] = field(compare=False, default_factory=dict)
    priority: RequestPriority = field(compare=False, default=RequestPriority.NORMAL)
    submitted_at: datetime = field(compare=False, default_factory=datetime.now)
    started_at: Optional[datetime] = field(compare=False, default=None)
    completed_at: Optional[datetime] = field(compare=False, default=None)
    status: RequestStatus = field(compare=False, default=RequestStatus.QUEUED)
    result: Optional[Any] = field(compare=False, default=None)
    error: Optional[str] = field(compare=False, default=None)
    job_id: Optional[int] = field(compare=False, default=None)

    @classmethod
    def create(
        cls,
        user_id: str,
        user_name: str,
        channel_id: str,
        playbook: str,
        inventory: str,
        extra_vars: Optional[Dict[str, Any]] = None,
        priority: RequestPriority = RequestPriority.NORMAL,
    ) -> "PlaybookRequest":
        """Factory method to create a new request."""
        request_id = str(uuid.uuid4())[:8]
        submitted = datetime.now()
        return cls(
            sort_key=(priority.value, submitted.timestamp()),
            id=request_id,
            user_id=user_id,
            user_name=user_name,
            channel_id=channel_id,
            playbook=playbook,
            inventory=inventory,
            extra_vars=extra_vars or {},
            priority=priority,
            submitted_at=submitted,
        )

    @property
    def dedup_key(self) -> str:
        """Key for deduplication - same playbook + inventory = duplicate."""
        return f"{self.playbook}:{self.inventory}"

    def to_slack_message(self) -> str:
        """Format request for Slack display."""
        status_emoji = {
            RequestStatus.QUEUED: "⏳",
            RequestStatus.RUNNING: "🔄",
            RequestStatus.COMPLETED: "✅",
            RequestStatus.FAILED: "❌",
            RequestStatus.DUPLICATE: "🔁",
            RequestStatus.CANCELLED: "🚫",
        }
        emoji = status_emoji.get(self.status, "❓")

        msg = f"{emoji} `{self.id}` | {self.playbook} on {self.inventory}"
        if self.user_name:
            msg += f" | by @{self.user_name}"
        if self.status == RequestStatus.QUEUED:
            msg += f" | Priority: {self.priority.name}"
        if self.job_id:
            msg += f" | Job: {self.job_id}"

        return msg


class RequestQueue:
    """
    Manages a queue of playbook requests with:
    - Priority-based ordering
    - Concurrent execution limits
    - Deduplication
    - User notifications
    """

    def __init__(
        self,
        max_concurrent: int = 3,
        notify_callback: Optional[Callable] = None,
    ):
        self.max_concurrent = max_concurrent
        self.notify_callback = notify_callback

        # Priority queue for pending requests
        self._pending: List[PlaybookRequest] = []

        # Currently running requests: dedup_key -> request
        self._running: Dict[str, PlaybookRequest] = {}

        # Completed requests (keep last 100)
        self._completed: List[PlaybookRequest] = []
        self._max_history = 100

        # User request tracking: user_id -> list of request_ids
        self._user_requests: Dict[str, List[str]] = defaultdict(list)

        # All requests by ID for lookup
        self._all_requests: Dict[str, PlaybookRequest] = {}

        # Lock for thread safety
        self._lock = asyncio.Lock()

        # Worker task
        self._worker_task: Optional[asyncio.Task] = None
        self._executor: Optional[Callable] = None

        logger.info(f"RequestQueue initialized with max_concurrent={max_concurrent}")

    async def start(self, executor: Callable):
        """Start the queue worker."""
        self._executor = executor
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("RequestQueue worker started")

    async def stop(self):
        """Stop the queue worker."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("RequestQueue worker stopped")

    async def submit(self, request: PlaybookRequest) -> Tuple[bool, str]:
        """
        Submit a request to the queue.

        Returns:
            (success, message) tuple
        """
        async with self._lock:
            # Check for duplicates in running
            if request.dedup_key in self._running:
                running_req = self._running[request.dedup_key]
                request.status = RequestStatus.DUPLICATE
                return False, (
                    f"🔁 *Duplicate Request*\n\n"
                    f"The same playbook is already running:\n"
                    f"• Playbook: `{request.playbook}`\n"
                    f"• Inventory: `{request.inventory}`\n"
                    f"• Started by: @{running_req.user_name}\n"
                    f"• Job ID: `{running_req.job_id or 'starting...'}`\n\n"
                    f"Wait for it to complete or check status with `job status {running_req.job_id}`"
                )

            # Check for duplicates in pending queue
            for pending in self._pending:
                if pending.dedup_key == request.dedup_key:
                    request.status = RequestStatus.DUPLICATE
                    return False, (
                        f"🔁 *Already Queued*\n\n"
                        f"This playbook is already in the queue:\n"
                        f"• Request ID: `{pending.id}`\n"
                        f"• Submitted by: @{pending.user_name}\n"
                        f"• Position: {self._pending.index(pending) + 1}\n\n"
                        f"Use `queue status` to check progress."
                    )

            # Add to queue
            heapq.heappush(self._pending, request)
            self._all_requests[request.id] = request
            self._user_requests[request.user_id].append(request.id)

            position = len(self._pending)
            running_count = len(self._running)

            logger.info(f"Request {request.id} queued: {request.playbook} on {request.inventory} "
                       f"(position={position}, running={running_count})")

            # Notify user
            if position == 1 and running_count < self.max_concurrent:
                return True, (
                    f"🚀 *Request Submitted*\n\n"
                    f"• Request ID: `{request.id}`\n"
                    f"• Playbook: `{request.playbook}`\n"
                    f"• Inventory: `{request.inventory}`\n"
                    f"• Priority: `{request.priority.name}`\n\n"
                    f"⏳ Starting immediately..."
                )
            else:
                return True, (
                    f"📋 *Request Queued*\n\n"
                    f"• Request ID: `{request.id}`\n"
                    f"• Playbook: `{request.playbook}`\n"
                    f"• Inventory: `{request.inventory}`\n"
                    f"• Priority: `{request.priority.name}`\n"
                    f"• Position in queue: `{position}`\n"
                    f"• Currently running: `{running_count}/{self.max_concurrent}`\n\n"
                    f"You'll be notified when it starts. Use `queue status` to check."
                )

    async def cancel(self, request_id: str, user_id: str) -> Tuple[bool, str]:
        """Cancel a pending request."""
        async with self._lock:
            if request_id not in self._all_requests:
                return False, f"Request `{request_id}` not found."

            request = self._all_requests[request_id]

            # Can only cancel own requests (unless admin)
            if request.user_id != user_id:
                return False, f"You can only cancel your own requests."

            if request.status == RequestStatus.RUNNING:
                return False, f"Request `{request_id}` is already running. Cannot cancel."

            if request.status != RequestStatus.QUEUED:
                return False, f"Request `{request_id}` is not in queue (status: {request.status.value})."

            # Remove from pending queue
            self._pending = [r for r in self._pending if r.id != request_id]
            heapq.heapify(self._pending)

            request.status = RequestStatus.CANCELLED
            self._completed.append(request)

            return True, f"✅ Request `{request_id}` cancelled."

    async def get_status(self) -> str:
        """Get overall queue status."""
        async with self._lock:
            lines = ["📊 *Queue Status*\n"]

            # Running
            lines.append(f"*Running:* {len(self._running)}/{self.max_concurrent}")
            if self._running:
                for req in self._running.values():
                    duration = ""
                    if req.started_at:
                        secs = (datetime.now() - req.started_at).seconds
                        duration = f" ({secs}s)"
                    lines.append(f"  🔄 `{req.id}` {req.playbook} on {req.inventory}{duration}")

            # Pending
            lines.append(f"\n*Queued:* {len(self._pending)}")
            for i, req in enumerate(sorted(self._pending)[:5]):
                lines.append(f"  {i+1}. `{req.id}` {req.playbook} on {req.inventory} [{req.priority.name}]")
            if len(self._pending) > 5:
                lines.append(f"  ... and {len(self._pending) - 5} more")

            # Recent completed
            recent = self._completed[-5:]
            if recent:
                lines.append(f"\n*Recent:*")
                for req in reversed(recent):
                    emoji = "✅" if req.status == RequestStatus.COMPLETED else "❌"
                    lines.append(f"  {emoji} `{req.id}` {req.playbook} - {req.status.value}")

            return "\n".join(lines)

    async def get_user_requests(self, user_id: str) -> str:
        """Get requests for a specific user."""
        async with self._lock:
            request_ids = self._user_requests.get(user_id, [])
            if not request_ids:
                return "You have no recent requests."

            lines = ["📋 *Your Requests*\n"]

            for req_id in request_ids[-10:]:  # Last 10
                req = self._all_requests.get(req_id)
                if req:
                    lines.append(req.to_slack_message())

            return "\n".join(lines)

    async def _worker_loop(self):
        """Background worker that processes the queue."""
        logger.info("Queue worker loop started")

        while True:
            try:
                await self._process_next()
                await asyncio.sleep(1)  # Check every second
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in queue worker: {e}")
                await asyncio.sleep(5)

    async def _process_next(self):
        """Process the next request if capacity available."""
        async with self._lock:
            # Check if we can run more
            if len(self._running) >= self.max_concurrent:
                return

            if not self._pending:
                return

            # Get highest priority request
            request = heapq.heappop(self._pending)

            # Double-check dedup (in case something changed)
            if request.dedup_key in self._running:
                request.status = RequestStatus.DUPLICATE
                self._completed.append(request)
                return

            # Mark as running
            request.status = RequestStatus.RUNNING
            request.started_at = datetime.now()
            self._running[request.dedup_key] = request

        # Execute outside the lock
        logger.info(f"Starting request {request.id}: {request.playbook} on {request.inventory}")

        # Notify user that their request is starting
        if self.notify_callback:
            try:
                await self.notify_callback(
                    channel_id=request.channel_id,
                    message=(
                        f"🚀 *Starting Request `{request.id}`*\n\n"
                        f"• Playbook: `{request.playbook}`\n"
                        f"• Inventory: `{request.inventory}`\n"
                        f"• Requested by: @{request.user_name}\n\n"
                        f"⏳ Running..."
                    )
                )
            except Exception as e:
                logger.error(f"Failed to notify start: {e}")

        # Execute the playbook
        try:
            if self._executor:
                result = await self._executor(
                    playbook=request.playbook,
                    inventory=request.inventory,
                    extra_vars=request.extra_vars,
                    user_id=request.user_id,
                    channel_id=request.channel_id,
                )
                request.result = result
                request.status = RequestStatus.COMPLETED
                if hasattr(result, 'data') and result.data:
                    request.job_id = result.data.get('job_id')
            else:
                request.status = RequestStatus.FAILED
                request.error = "No executor configured"
        except Exception as e:
            logger.exception(f"Error executing request {request.id}")
            request.status = RequestStatus.FAILED
            request.error = str(e)

        # Complete the request
        async with self._lock:
            request.completed_at = datetime.now()
            del self._running[request.dedup_key]
            self._completed.append(request)

            # Trim history
            if len(self._completed) > self._max_history:
                self._completed = self._completed[-self._max_history:]

        # Notify completion
        if self.notify_callback and request.result:
            try:
                await self.notify_callback(
                    channel_id=request.channel_id,
                    message=request.result.message if hasattr(request.result, 'message') else str(request.result),
                )
            except Exception as e:
                logger.error(f"Failed to notify completion: {e}")


# Global queue instance
_queue: Optional[RequestQueue] = None


def get_queue() -> RequestQueue:
    """Get the global request queue instance."""
    global _queue
    if _queue is None:
        _queue = RequestQueue(max_concurrent=3)
    return _queue


def set_queue(queue: RequestQueue):
    """Set a custom queue instance."""
    global _queue
    _queue = queue
