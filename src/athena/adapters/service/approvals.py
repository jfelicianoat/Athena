"""Remote human approval, with the three clocks ADR-017 separates.

An approval timeout measures how long a *person* takes to decide. It is not a network
timeout. A slow model or a slow link delays the request; it must never eat the human's
thinking time, because a false denial throws away a whole run.

So the wait is split:

- nobody attached           -> deny at once, which is Athena's unattended default anyway;
- attached, not yet shown   -> a short delivery window;
- confirmed on screen       -> the full human window, which only starts on acknowledgement.

Everything else about approval is unchanged from the console path: single use, no standing
grant, and an answer that arrives after cancellation is refused rather than applied.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from athena.errors import AthenaRuntimeError
from athena.permissions import PermissionDecision, PermissionRequest
from athena.security import redact_sensitive
from athena.types import JSONObject, JSONValue

DEFAULT_DELIVERY_TIMEOUT_SECONDS = 30.0
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_CONSECUTIVE_TIMEOUTS = 3

#: How much of a long argument value a human needs in order to judge the request.
_ARGUMENT_PREVIEW_CHARS = 200


def sanitise_arguments(arguments: JSONObject) -> JSONObject:
    """Make a tool's arguments safe and useful to show a person.

    Two different problems, both real. `write_file` carries an entire file in
    `content`, so shipping arguments verbatim would push a whole payload through
    the event stream and into a UI that only needs to know how big it is. And an
    argument can carry a secret, which must never leave the process in clear.

    So every value is either passed through, summarised by size, or redacted —
    and the whole thing goes through the same redaction the event bus applies,
    because an approval is published straight to subscribers and would otherwise
    skip it.
    """
    summarised: dict[str, JSONValue] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > _ARGUMENT_PREVIEW_CHARS:
            summarised[key] = {
                "preview": value[:_ARGUMENT_PREVIEW_CHARS],
                "chars": len(value),
                "truncated": True,
            }
        else:
            summarised[key] = value
    redacted = redact_sensitive(summarised)
    return redacted if isinstance(redacted, dict) else {}


class ApprovalAbandonedError(AthenaRuntimeError):
    """Repeated approval requests went unanswered, so nobody is coming back."""

    code = "approval_abandoned"


@dataclass(slots=True)
class PendingApproval:
    """One question waiting for one answer."""

    request_id: str
    run_id: str
    request: PermissionRequest
    future: asyncio.Future[PermissionDecision]
    deadline_monotonic: float
    acknowledged: bool = False
    #: Set once resolved, so a duplicate POST cannot approve a second action.
    consumed: bool = False
    created_monotonic: float = field(default_factory=time.monotonic)

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def to_json(self) -> JSONObject:
        request = self.request
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "tool_name": request.tool_name,
            "operation": request.operation,
            "action": request.action,
            "risk": request.risk.value,
            "tier": request.tier.value,
            "reason": request.reason,
            "possible_effects": list(request.possible_effects),
            "resources": list(request.resources),
            "is_read_only": request.is_read_only,
            "is_destructive": request.is_destructive,
            "is_concurrency_safe": request.is_concurrency_safe,
            "workspace": str(request.workspace.root),
            "arguments": sanitise_arguments(request.arguments),
            "acknowledged": self.acknowledged,
            "seconds_remaining": round(self.seconds_remaining, 1),
        }


class ApprovalRegistry:
    """Holds the questions in flight and matches answers to them."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}

    def register(self, pending: PendingApproval) -> None:
        self._pending[pending.request_id] = pending

    def get(self, request_id: str) -> PendingApproval | None:
        return self._pending.get(request_id)

    def pending_for(self, run_id: str) -> tuple[PendingApproval, ...]:
        return tuple(
            item for item in self._pending.values() if item.run_id == run_id and not item.consumed
        )

    def discard(self, request_id: str) -> None:
        self._pending.pop(request_id, None)

    def acknowledge(self, request_id: str, human_window_seconds: float) -> PendingApproval | None:
        """The client says the prompt is on screen. Only now does the human clock start."""
        pending = self._pending.get(request_id)
        if pending is None or pending.consumed or pending.acknowledged:
            return pending
        pending.acknowledged = True
        pending.deadline_monotonic = time.monotonic() + human_window_seconds
        return pending

    def resolve(self, request_id: str, decision: PermissionDecision) -> PendingApproval | None:
        """Apply an answer exactly once."""
        pending = self._pending.get(request_id)
        if pending is None or pending.consumed:
            return None
        pending.consumed = True
        if not pending.future.done():
            # Anything that is not an explicit ALLOW is a refusal.
            pending.future.set_result(
                decision if decision is PermissionDecision.ALLOW else PermissionDecision.DENY
            )
        return pending

    def cancel_run(self, run_id: str) -> None:
        """A cancelled run's questions are withdrawn; a late answer must not apply."""
        for pending in tuple(self._pending.values()):
            if pending.run_id != run_id:
                continue
            pending.consumed = True
            if not pending.future.done():
                pending.future.set_result(PermissionDecision.DENY)


class RemotePermissionPrompt:
    """`PermissionPrompt` implementation that asks a connected client.

    `has_client` is supplied by the run registry rather than assumed, because "is anyone
    watching this run" is a fact about connections, not about the prompt.
    """

    def __init__(
        self,
        registry: ApprovalRegistry,
        run_id: str,
        publish: Callable[[PendingApproval], object],
        has_client: Callable[[], bool],
        *,
        delivery_timeout_seconds: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
        approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        max_consecutive_timeouts: int = DEFAULT_MAX_CONSECUTIVE_TIMEOUTS,
    ) -> None:
        if delivery_timeout_seconds <= 0 or approval_timeout_seconds <= 0:
            raise ValueError("Approval windows must be positive")
        if max_consecutive_timeouts < 1:
            raise ValueError("max_consecutive_timeouts must be at least 1")
        self.registry = registry
        self.run_id = run_id
        self.publish = publish
        self.has_client = has_client
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self.approval_timeout_seconds = approval_timeout_seconds
        self.max_consecutive_timeouts = max_consecutive_timeouts
        self.consecutive_timeouts = 0

    async def confirm(self, request: PermissionRequest) -> PermissionDecision:
        if not self.has_client():
            # Unattended. Athena's default has always been to refuse rather than guess.
            return PermissionDecision.DENY

        loop = asyncio.get_running_loop()
        pending = PendingApproval(
            request_id=str(uuid4()),
            run_id=self.run_id,
            request=request,
            future=loop.create_future(),
            deadline_monotonic=time.monotonic() + self.delivery_timeout_seconds,
        )
        self.registry.register(pending)
        try:
            self.publish(pending)
            decision = await self._await_decision(pending)
        finally:
            self.registry.discard(pending.request_id)

        if decision is PermissionDecision.ALLOW:
            self.consecutive_timeouts = 0
        return decision

    async def _await_decision(self, pending: PendingApproval) -> PermissionDecision:
        # First window: has the prompt reached a screen at all?
        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.future), timeout=self.delivery_timeout_seconds
            )
        except TimeoutError:
            pass
        if not pending.acknowledged:
            return self._timed_out("The approval request was never displayed")

        # Second window: the human is looking, so give them the time to decide.
        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.future), timeout=pending.seconds_remaining or 0.01
            )
        except TimeoutError:
            return self._timed_out("The approval request was not answered in time")

    def _timed_out(self, reason: str) -> PermissionDecision:
        self.consecutive_timeouts += 1
        if self.consecutive_timeouts >= self.max_consecutive_timeouts:
            raise ApprovalAbandonedError(
                f"{self.consecutive_timeouts} approval requests went unanswered; "
                "abandoning the run rather than spending its budget refusing itself",
                details={"run_id": self.run_id, "reason": reason},
            )
        return PermissionDecision.DENY


__all__ = [
    "DEFAULT_APPROVAL_TIMEOUT_SECONDS",
    "DEFAULT_DELIVERY_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CONSECUTIVE_TIMEOUTS",
    "ApprovalAbandonedError",
    "ApprovalRegistry",
    "PendingApproval",
    "RemotePermissionPrompt",
]
