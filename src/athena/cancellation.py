"""Cooperative cancellation primitives shared across runtime boundaries.

Cancellation is hierarchical because the runtime is. A run holds subgraphs, a subgraph
holds tasks, a task holds a loop, a loop holds tools, a tool holds processes — and stopping
any level has to stop everything under it and nothing above it. `CancellationSource.child`
is that relationship made explicit rather than re-implemented at each boundary, which is
what `TaskManager` and `BackgroundProcess` were each doing separately.

A cancellation also carries *why*. "Someone asked" and "the clock ran out" end a run in
visibly different ways, and a runtime that collapses them tells a person their work was
abandoned when in fact it exceeded a limit they set.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event, Lock

from athena.errors import CancellationError

CancellationCallback = Callable[[], None]


class CancellationScope(StrEnum):
    """What a cancellation is aimed at.

    The scope is a fact about the source, not a request to the receiver: a token does not
    decide how far a stop travels — the shape of the source tree already did.
    """

    RUN = "run"
    SUBGRAPH = "subgraph"
    TASK = "task"


class CancellationReason(StrEnum):
    REQUESTED = "requested"
    TIMED_OUT = "timed_out"
    #: This scope did not end on its own account; the one above it did.
    PARENT_CANCELLED = "parent_cancelled"


@dataclass
class _CancellationState:
    event: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    callbacks: dict[int, CancellationCallback] = field(default_factory=dict)
    next_callback_id: int = 0
    scope: CancellationScope = CancellationScope.RUN
    reason: CancellationReason | None = None


class CancellationToken:
    """Read-only view of a cancellation signal."""

    def __init__(self, state: _CancellationState) -> None:
        self._state = state

    @property
    def is_cancelled(self) -> bool:
        return self._state.event.is_set()

    @property
    def scope(self) -> CancellationScope:
        return self._state.scope

    @property
    def reason(self) -> CancellationReason | None:
        """Why this was cancelled, or `None` while it still stands."""
        return self._state.reason

    def raise_if_cancelled(self) -> None:
        if not self.is_cancelled:
            return
        reason = self._state.reason or CancellationReason.REQUESTED
        raise CancellationError(
            f"Operation was cancelled ({reason.value})",
            details={"scope": self._state.scope.value, "reason": reason.value},
        )

    async def wait(self) -> None:
        while not self._state.event.is_set():
            await asyncio.sleep(0.01)

    def register(self, callback: CancellationCallback) -> Callable[[], None]:
        """Register a propagation callback and return an idempotent unsubscribe."""
        with self._state.lock:
            if self._state.event.is_set():
                invoke_now = True
                callback_id = -1
            else:
                invoke_now = False
                callback_id = self._state.next_callback_id
                self._state.next_callback_id += 1
                self._state.callbacks[callback_id] = callback
        if invoke_now:
            callback()

        def unsubscribe() -> None:
            with self._state.lock:
                self._state.callbacks.pop(callback_id, None)

        return unsubscribe


class CancellationSource:
    """Owner that can signal one or more token consumers exactly once."""

    def __init__(self, scope: CancellationScope = CancellationScope.RUN) -> None:
        self._state = _CancellationState(scope=scope)
        self.token = CancellationToken(self._state)

    @property
    def scope(self) -> CancellationScope:
        return self._state.scope

    def cancel(self, reason: CancellationReason = CancellationReason.REQUESTED) -> None:
        with self._state.lock:
            if self._state.event.is_set():
                return
            self._state.event.set()
            self._state.reason = reason
            callbacks = tuple(self._state.callbacks.values())
            self._state.callbacks.clear()
        for callback in callbacks:
            callback()

    def child(self, scope: CancellationScope) -> CancellationSource:
        """A source that this one can stop, and that cannot stop this one.

        The asymmetry is the whole point. Cancelling one task must not take down the run
        that owns it, and cancelling the run must take down every task under it — which is
        one registration, done here once, instead of the same wiring repeated at every
        level that happens to own something.
        """
        return chained_source(self.token, scope)


def chained_source(parent: CancellationToken, scope: CancellationScope) -> CancellationSource:
    """A source the parent token can stop, taking a token rather than a source.

    The primitive is expressed over a token because that is what gets handed down: a level
    that owns work is given the right to observe its parent's cancellation, not the right
    to cause it.
    """
    nested = CancellationSource(scope)
    parent.register(lambda: nested.cancel(CancellationReason.PARENT_CANCELLED))
    return nested


__all__ = [
    "CancellationCallback",
    "CancellationReason",
    "CancellationScope",
    "CancellationSource",
    "CancellationToken",
    "chained_source",
]
