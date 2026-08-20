"""Extension points that observe and restrict, but never grant.

A hook can watch what the runtime is about to do, and it can refuse. It cannot approve.
That asymmetry is the whole design: `PermissionEngine` remains the only thing that can say
yes, so no extension — however well-intentioned — can widen Athena's authority by being
installed. A hook that blocks is adding a restriction, which is always safe; a hook that
could unblock would be a second, unaudited permission system.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.errors import AthenaRuntimeError
from athena.types import JSONObject


class HookEvent(StrEnum):
    SESSION_START = "SessionStart"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_EDIT = "PreEdit"
    POST_EDIT = "PostEdit"
    ON_ERROR = "OnError"
    PRE_VERIFY = "PreVerify"
    POST_VERIFY = "PostVerify"
    SESSION_END = "SessionEnd"


class HookDecision(StrEnum):
    #: Carry on. The only outcome an observational hook can produce.
    CONTINUE = "continue"
    #: Refuse this action. Deliberately the only power a hook has over the runtime.
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class HookContext:
    """What a hook is told. It receives facts, not the ability to change them."""

    event: HookEvent
    session_id: str
    payload: JSONObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HookResult:
    decision: HookDecision = HookDecision.CONTINUE
    reason: str = ""
    #: Advisory notes surfaced to the runtime; they never alter control flow.
    notes: tuple[str, ...] = ()

    @property
    def blocks(self) -> bool:
        return self.decision is HookDecision.BLOCK


HookCallable = Callable[[HookContext], HookResult | Awaitable[HookResult | None] | None]


@dataclass(frozen=True, slots=True)
class Hook:
    """One registered extension.

    `blocking` decides what happens when the hook itself fails. A blocking hook is a guard,
    so a guard that crashes must refuse rather than wave the action through; an
    observational hook that crashes is recorded and ignored.
    """

    name: str
    event: HookEvent
    handler: HookCallable
    blocking: bool = False
    #: Lower runs first. Ties keep registration order.
    order: int = 100


@dataclass(frozen=True, slots=True)
class HookReport:
    event: HookEvent
    ran: tuple[str, ...] = ()
    blocked_by: str | None = None
    reason: str = ""
    failures: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.blocked_by is not None


class HookBlockedError(AthenaRuntimeError):
    """A hook refused the action. Recovery treats it like a permission refusal."""

    code = "hook_blocked"


@runtime_checkable
class HookHost(Protocol):
    async def run(self, context: HookContext) -> HookReport: ...


class HookRegistry:
    """Ordered, fail-explicit hook dispatch.

    Hooks for one event run in `order`, then registration order. The first BLOCK wins and
    the remaining hooks for that event do not run: the action is already refused, and
    running further guards would only produce noise.
    """

    def __init__(self, hooks: Iterable[Hook] = ()) -> None:
        self._hooks: list[Hook] = []
        for hook in hooks:
            self.register(hook)

    def register(self, hook: Hook) -> None:
        if any(existing.name == hook.name for existing in self._hooks):
            raise ValueError(f"Hook already registered: {hook.name}")
        self._hooks.append(hook)
        self._hooks.sort(key=lambda item: item.order)

    def for_event(self, event: HookEvent) -> tuple[Hook, ...]:
        return tuple(hook for hook in self._hooks if hook.event is event)

    def names(self) -> tuple[str, ...]:
        return tuple(hook.name for hook in self._hooks)

    async def run(self, context: HookContext) -> HookReport:
        ran: list[str] = []
        failures: list[tuple[str, str]] = []
        notes: list[str] = []
        for hook in self.for_event(context.event):
            ran.append(hook.name)
            try:
                outcome = hook.handler(context)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            except Exception as exc:
                failures.append((hook.name, f"{type(exc).__name__}: {exc}"))
                if hook.blocking:
                    return HookReport(
                        context.event,
                        tuple(ran),
                        blocked_by=hook.name,
                        reason=f"Blocking hook failed: {type(exc).__name__}: {exc}",
                        failures=tuple(failures),
                        notes=tuple(notes),
                    )
                continue
            if outcome is None:
                continue
            notes.extend(outcome.notes)
            if outcome.blocks:
                return HookReport(
                    context.event,
                    tuple(ran),
                    blocked_by=hook.name,
                    reason=outcome.reason or f"Blocked by {hook.name}",
                    failures=tuple(failures),
                    notes=tuple(notes),
                )
        return HookReport(context.event, tuple(ran), failures=tuple(failures), notes=tuple(notes))


__all__ = [
    "Hook",
    "HookBlockedError",
    "HookContext",
    "HookDecision",
    "HookEvent",
    "HookHost",
    "HookRegistry",
    "HookReport",
    "HookResult",
]
