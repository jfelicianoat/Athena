"""Cooperative cancellation primitives shared across runtime boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, Lock

from athena.errors import CancellationError

CancellationCallback = Callable[[], None]


@dataclass
class _CancellationState:
    event: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    callbacks: dict[int, CancellationCallback] = field(default_factory=dict)
    next_callback_id: int = 0


class CancellationToken:
    """Read-only view of a cancellation signal."""

    def __init__(self, state: _CancellationState) -> None:
        self._state = state

    @property
    def is_cancelled(self) -> bool:
        return self._state.event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CancellationError("Operation was cancelled")

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

    def __init__(self) -> None:
        self._state = _CancellationState()
        self.token = CancellationToken(self._state)

    def cancel(self) -> None:
        with self._state.lock:
            if self._state.event.is_set():
                return
            self._state.event.set()
            callbacks = tuple(self._state.callbacks.values())
            self._state.callbacks.clear()
        for callback in callbacks:
            callback()
