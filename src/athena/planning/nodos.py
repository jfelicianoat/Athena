"""Un nodo del plan y los limites que impiden que el plan crezca solo.

Estan aparte de `grafo` porque los necesitan tanto el grafo como sus
validaciones, y un modulo compartido evita la dependencia circular.
"""

from __future__ import annotations

from dataclasses import dataclass

from athena.planning.estados import PlanStatus
from athena.subagents import SubagentRole
from athena.types import JSONObject


@dataclass(frozen=True, slots=True)
class TaskNode:
    """One unit of a plan.

    `acceptance_criteria` is not decoration. A task nobody can check is a task that will be
    reported finished on the model's word, which is the one thing ADR-012 exists to
    prevent — so a leaf without criteria is rejected rather than accepted optimistically.
    """

    id: str
    goal: str
    expected_output: str
    parent_id: str | None = None
    inputs: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    suggested_role: SubagentRole = SubagentRole.CODER
    toolsets: tuple[str, ...] = ()
    status: PlanStatus = PlanStatus.PENDING
    attempts: int = 0
    #: What the last attempt proved, or `None` if nothing has been proven yet.
    verification: JSONObject | None = None

    def to_json(self) -> JSONObject:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "goal": self.goal,
            "inputs": list(self.inputs),
            "expected_output": self.expected_output,
            "acceptance_criteria": list(self.acceptance_criteria),
            "dependencies": list(self.dependencies),
            "suggested_role": self.suggested_role.value,
            "toolsets": list(self.toolsets),
            "status": self.status.value,
            "attempts": self.attempts,
            "verification": self.verification,
        }


@dataclass(frozen=True, slots=True)
class PlanningLimits:
    """What stops a plan from planning.

    A model asked to decompose will decompose again if invited to, and the invitation is
    implicit in every "is this atomic yet?". These are the boundaries that make the answer
    eventually yes regardless of what the model thinks.
    """

    max_depth: int = 3
    max_tasks: int = 32
    max_children: int = 8
    #: Total attempts the whole graph may spend. Distinct from a per-task retry limit: a
    #: plan can also fail by spreading one failure thinly across many tasks.
    max_total_attempts: int = 64

    def __post_init__(self) -> None:
        for name in ("max_depth", "max_tasks", "max_children", "max_total_attempts"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


# --------------------------------------------------------------------------- the graph
