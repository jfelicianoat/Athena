"""Errores de planificacion y estados de una tarea.

Las transiciones estan declaradas en una tabla y no repartidas en ifs: un
estado nuevo se anade en un sitio y el resto del codigo lo respeta solo.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from athena.errors import AthenaRuntimeError

# --------------------------------------------------------------------------- errors


class PlanningError(AthenaRuntimeError):
    """A plan was refused. Never raised for a plan that is merely ambitious."""

    code = "planning_error"


class DuplicateTaskIdError(PlanningError):
    code = "plan_duplicate_task_id"


class UnknownDependencyError(PlanningError):
    code = "plan_unknown_dependency"


class CyclicPlanError(PlanningError):
    code = "plan_cycle"


class PlanLimitExceededError(PlanningError):
    code = "plan_limit_exceeded"


class InvalidTransitionError(PlanningError):
    code = "plan_invalid_transition"


class NonAtomicTaskError(PlanningError):
    code = "plan_task_not_atomic"


class RedundantTaskError(PlanningError):
    code = "plan_redundant_task"


class PlanParseError(PlanningError):
    """The model returned something that is not a plan. It is not asked twice here."""

    code = "plan_unparseable"


# --------------------------------------------------------------------------- statuses


class PlanStatus(StrEnum):
    """Where a task sits in the plan.

    Separate from `TaskState` in `athena.tasks`, which describes a *running* thing and
    carries `killed` and `recovery_pending`. Collapsing the two would lose the distinction
    between "the plan says this cannot start yet" and "the process was killed", which is
    the same argument `tasks.py` makes for having seven states of its own.
    """

    PENDING = "pending"
    #: Every dependency is satisfied. Nothing has started.
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    #: A dependency failed, so this can never start as things stand.
    BLOCKED = "blocked"
    #: Deliberately not attempted — collapsed away, or made irrelevant by replanning.
    SKIPPED = "skipped"
    #: The process died while this was running. Nobody knows whether it finished, and the
    #: same reasoning `AgentStatus` and `TaskState` use applies: the unknown case resolves
    #: towards "needs a decision", never towards "done".
    RECOVERY_PENDING = "recovery_pending"


#: What may follow what. A plan whose statuses can move arbitrarily is a plan whose state
#: cannot be trusted to mean anything, and "verified" is exactly the value worth protecting:
#: nothing reaches `COMPLETED` except from `RUNNING`.
_TRANSITIONS: Mapping[PlanStatus, frozenset[PlanStatus]] = {
    PlanStatus.PENDING: frozenset({PlanStatus.READY, PlanStatus.BLOCKED, PlanStatus.SKIPPED}),
    PlanStatus.READY: frozenset({PlanStatus.RUNNING, PlanStatus.BLOCKED, PlanStatus.SKIPPED}),
    PlanStatus.RUNNING: frozenset(
        {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.RECOVERY_PENDING}
    ),
    #: A completed task can be reopened only by replanning, which sends it back to PENDING.
    PlanStatus.COMPLETED: frozenset({PlanStatus.PENDING}),
    PlanStatus.FAILED: frozenset({PlanStatus.PENDING, PlanStatus.SKIPPED}),
    PlanStatus.BLOCKED: frozenset({PlanStatus.PENDING, PlanStatus.SKIPPED}),
    PlanStatus.SKIPPED: frozenset({PlanStatus.PENDING}),
    #: A person or an operator decides. It never resolves itself, which is the point.
    PlanStatus.RECOVERY_PENDING: frozenset(
        {PlanStatus.PENDING, PlanStatus.SKIPPED, PlanStatus.FAILED}
    ),
}

_TERMINAL = frozenset({PlanStatus.COMPLETED, PlanStatus.SKIPPED})


# --------------------------------------------------------------------------- the model
