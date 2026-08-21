"""Structured operational state.

Conversation history is contextual input, not runtime state. What Athena knows about
its own run lives here, in typed fields the runtime validates on every update, so that
recovery, verification and interfaces can reason over it without re-reading a transcript.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import TypeVar

from athena.errors import ToolValidationError
from athena.types import JSONObject

T = TypeVar("T")

#: Bounds keep the state summarisable; the newest entries win when a list overflows.
_MAX_LIST_ENTRIES = 100
_MAX_TEXT_CHARS = 2_000


class StepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class PlanStep:
    description: str
    status: StepStatus = StepStatus.PENDING
    #: The task this step came from, when it came from one.
    #:
    #: A plan the loop wrote for itself has no ids — its steps are prose, and inventing
    #: identifiers for them would let something downstream believe it can address one.
    #: A plan that came from a `TaskGraph` does, and carrying it is what lets a client
    #: that reconnects match the steps in the snapshot to the task events it goes on to
    #: receive, instead of drawing the same task twice.
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecordedError:
    code: str
    message: str
    recovery_action: str | None = None


@dataclass(frozen=True, slots=True)
class WorkingState:
    """The minimum a coding agent must remember outside its own transcript."""

    objective: str
    constraints: tuple[str, ...] = ()
    current_plan: tuple[PlanStep, ...] = ()
    current_step: int | None = None
    facts: tuple[str, ...] = ()
    files_examined: tuple[str, ...] = ()
    files_modified: tuple[str, ...] = ()
    commands_run: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    errors: tuple[RecordedError, ...] = ()
    verification: JSONObject = field(default_factory=dict)
    remaining_work: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ToolValidationError("Working state requires a non-empty objective")
        self._validate_step(self.current_step, self.current_plan)

    @staticmethod
    def _validate_step(step: int | None, plan: tuple[PlanStep, ...]) -> None:
        if step is None:
            return
        if not plan:
            raise ToolValidationError("current_step was set without a plan")
        if step < 0 or step >= len(plan):
            raise ToolValidationError(
                f"current_step {step} is outside the plan of {len(plan)} step(s)"
            )

    # -- validated updates ------------------------------------------------

    def with_plan(
        self, steps: tuple[PlanStep, ...], current_step: int | None = None
    ) -> WorkingState:
        self._validate_step(current_step, steps)
        return replace(self, current_plan=steps, current_step=current_step)

    def advance_to(self, step: int) -> WorkingState:
        self._validate_step(step, self.current_plan)
        return replace(self, current_step=step)

    def observing(self, *, files_examined: tuple[str, ...] = ()) -> WorkingState:
        return replace(
            self, files_examined=_extend(self.files_examined, files_examined, unique=True)
        )

    def modifying(self, *, files_modified: tuple[str, ...] = ()) -> WorkingState:
        return replace(
            self, files_modified=_extend(self.files_modified, files_modified, unique=True)
        )

    def ran(self, command: str) -> WorkingState:
        return replace(self, commands_run=_extend(self.commands_run, (_bounded(command),)))

    def noting(
        self,
        *,
        facts: tuple[str, ...] = (),
        decisions: tuple[str, ...] = (),
        remaining_work: tuple[str, ...] | None = None,
    ) -> WorkingState:
        updated = replace(
            self,
            facts=_extend(self.facts, tuple(_bounded(item) for item in facts)),
            decisions=_extend(self.decisions, tuple(_bounded(item) for item in decisions)),
        )
        if remaining_work is None:
            return updated
        return replace(updated, remaining_work=tuple(_bounded(item) for item in remaining_work))

    def failing(self, error: RecordedError) -> WorkingState:
        return replace(self, errors=_extend(self.errors, (error,)))

    def verified(self, verification: JSONObject) -> WorkingState:
        return replace(self, verification=dict(verification))

    # -- serialisation ----------------------------------------------------

    def to_json(self) -> JSONObject:
        return {
            "objective": self.objective,
            "constraints": list(self.constraints),
            "current_plan": [asdict(step) for step in self.current_plan],
            "current_step": self.current_step,
            "facts": list(self.facts),
            "files_examined": list(self.files_examined),
            "files_modified": list(self.files_modified),
            "commands_run": list(self.commands_run),
            "decisions": list(self.decisions),
            "errors": [asdict(error) for error in self.errors],
            "verification": dict(self.verification),
            "remaining_work": list(self.remaining_work),
        }

    @classmethod
    def from_json(cls, payload: JSONObject) -> WorkingState:
        """Rebuild from storage, tolerating fields written by an older version."""
        objective = payload.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise ToolValidationError("Stored working memory has no usable objective")
        plan = tuple(
            PlanStep(
                description=str(step.get("description", "")),
                status=StepStatus(str(step.get("status", StepStatus.PENDING.value))),
                task_id=_optional_str(step.get("task_id")),
            )
            for step in _objects(payload.get("current_plan"))
        )
        errors = tuple(
            RecordedError(
                code=str(item.get("code", "unknown")),
                message=str(item.get("message", "")),
                recovery_action=_optional_str(item.get("recovery_action")),
            )
            for item in _objects(payload.get("errors"))
        )
        step = payload.get("current_step")
        current_step = step if isinstance(step, int) and not isinstance(step, bool) else None
        verification = payload.get("verification")
        return cls(
            objective=objective,
            constraints=_strings(payload.get("constraints")),
            current_plan=plan,
            current_step=current_step if current_step is not None and plan else None,
            facts=_strings(payload.get("facts")),
            files_examined=_strings(payload.get("files_examined")),
            files_modified=_strings(payload.get("files_modified")),
            commands_run=_strings(payload.get("commands_run")),
            decisions=_strings(payload.get("decisions")),
            errors=errors,
            verification=dict(verification) if isinstance(verification, Mapping) else {},
            remaining_work=_strings(payload.get("remaining_work")),
        )

    def summary(self) -> str:
        """Compact, model-facing rendering. Never the whole transcript."""
        plan = [
            f"{index}. [{step.status.value}] {step.description}"
            for index, step in enumerate(self.current_plan)
        ]
        payload = {
            "objective": self.objective,
            "constraints": list(self.constraints),
            "plan": plan,
            "current_step": self.current_step,
            "facts": list(self.facts[-10:]),
            "files_examined": list(self.files_examined[-20:]),
            "files_modified": list(self.files_modified),
            "commands_run": list(self.commands_run[-5:]),
            "decisions": list(self.decisions[-5:]),
            "errors": [f"{error.code}: {error.message}" for error in self.errors[-5:]],
            "verification": dict(self.verification),
            "remaining_work": list(self.remaining_work),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


def _bounded(value: str) -> str:
    if len(value) <= _MAX_TEXT_CHARS:
        return value
    return value[:_MAX_TEXT_CHARS] + "…"


def _extend(
    current: tuple[T, ...], additions: Sequence[T], *, unique: bool = False
) -> tuple[T, ...]:
    """Append with an upper bound; the newest entries survive an overflow."""
    combined = [*current, *additions]
    if unique:
        combined = list(dict.fromkeys(combined))
    return tuple(combined[-_MAX_LIST_ENTRIES:])


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _objects(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
