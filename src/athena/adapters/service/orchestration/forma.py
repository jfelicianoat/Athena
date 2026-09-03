"""Que forma tiene un run: ajustes, modo de ejecucion y por que se decidio asi.

La forma se razona y se deja escrita. Un run jerarquico que nadie sabe por
que lo es se convierte en folclore la primera vez que falla.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from enum import StrEnum

from athena.checkpoints import CheckpointStore
from athena.graph_store import SqliteGraphStore
from athena.planning import (
    DecompositionDecision,
    DecompositionPolicy,
    DecompositionSignals,
    PlanBoard,
    PlanningLimits,
    PlanStatus,
)
from athena.project_memory import (
    SqliteProjectMemory,
)
from athena.types import JSONObject
from athena.working_state import StepStatus

_logger = logging.getLogger(__name__)

#: How much remembered context a run is given. Selective, because a prompt carrying every
#: fact Athena ever recorded would spend the window on things this run has no use for.
_RECALLED = 6

#: A plan status as the working state records it. The two vocabularies are deliberately
#: different — `PlanStatus` describes a node in a graph, `StepStatus` describes a line a
#: person reads — so the mapping is stated once here rather than guessed at each use.
_AS_STEP = {
    PlanStatus.COMPLETED: StepStatus.DONE,
    PlanStatus.RUNNING: StepStatus.IN_PROGRESS,
    PlanStatus.FAILED: StepStatus.BLOCKED,
    PlanStatus.BLOCKED: StepStatus.BLOCKED,
}


@dataclass(frozen=True, slots=True)
class OrchestrationSettings:
    """What the service is willing and able to do beyond a single loop.

    Every field is optional and absent means "as before". A deployment that wants the V0.1
    behaviour gets it by configuring nothing, which is the only way to add a layer to a
    working system without betting the working system on it.
    """

    planning: bool = False
    limits: PlanningLimits = field(default_factory=PlanningLimits)
    policy: DecompositionPolicy = field(default_factory=DecompositionPolicy)
    memory: SqliteProjectMemory | None = None
    #: Donde se guardan las copias previas a una escritura, si el despliegue quiere poder
    #: deshacer. Sin ella no se copia nada y no hay nada que deshacer, que es exactamente
    #: lo que pasaba antes: `rollback.py` existia entero y no lo importaba nadie.
    checkpoints: CheckpointStore | None = None
    graphs: SqliteGraphStore | None = None
    board: PlanBoard | None = None
    #: Cuánto puede durar una tarea del plan, si el despliegue lo sabe mejor que el
    #: perfil. Los presupuestos por defecto —cinco minutos para explorar, diez para
    #: escribir— se escribieron pensando en modelos que contestan en segundos, y una sola
    #: llamada a un modelo local de 30B medida contra este broker tardó nueve minutos.
    #: `None` deja el presupuesto del perfil, que es lo correcto cuando nadie mide nada.
    task_timeout_seconds: float | None = None


class ExecutionMode(StrEnum):
    """What the caller wants the runtime to do with a goal.

    Three named modes rather than a boolean with three states. `hierarchical: null` reads
    as "unset", which a client cannot tell apart from "not supported" or "left at the
    default" — and the difference decides whether a run is planned at all.
    """

    #: Let the evidence decide. The normal way to run Athena.
    AUTO = "auto"
    #: Always execute through a `TaskGraph`, even if the plan holds a single task. Costs
    #: hand-offs a simple goal does not need, and is worth it when the graph itself is
    #: what is being observed: tests, debugging, benchmarks, experiments.
    HIERARCHICAL = "hierarchical"
    #: Always the loop, whatever the repository looks like.
    DIRECT = "direct"


class ShapeReason(StrEnum):
    """Why a run ended up with the shape it has, in a value that survives rewording.

    The sentences beside these are for people and will be rewritten; a count of how often
    a deployment falls back for want of a planner must not depend on the wording holding
    still. Anything that aggregates reads this and never the prose.
    """

    #: The caller required the loop.
    CALLER_REQUIRED_DIRECT = "caller_required_direct"
    #: The caller required a graph.
    CALLER_REQUIRED_HIERARCHICAL = "caller_required_hierarchical"
    #: `auto`, and this deployment has no planning layer to offer.
    PLANNING_UNAVAILABLE = "planning_unavailable"
    #: `auto`, and the evidence about the goal did not argue for decomposing it.
    POLICY_DECLINED = "policy_declined"
    #: `auto`, and the plan that came back is worth executing as a graph.
    POLICY_ENDORSED = "policy_endorsed"
    #: `auto`, and the plan is valid but buys nothing a loop does not already do.
    PLAN_NOT_WORTHWHILE = "plan_not_worthwhile"
    #: `auto`, and no usable plan came back, so the goal runs directly.
    PLAN_REFUSED = "plan_refused"
    #: `hierarchical`, and no usable plan came back, so the whole goal is one task.
    NO_USABLE_PLAN = "no_usable_plan"


@dataclass(frozen=True, slots=True)
class RunShape:
    """How this run will be executed, and why.

    `mode` is what was asked for and `hierarchical` is what will happen. They agree except
    in `AUTO`, where the second is the answer to a question the first only posed.

    `reason` is separate from `decision.explanation` because they answer different
    questions and can disagree. A deployment with planning switched off runs a goal on the
    loop while the policy still holds that decomposing it was worth it — and reporting the
    policy's sentence there would have the run explain itself with a verdict that is not
    the one it acted on.
    """

    mode: ExecutionMode
    hierarchical: bool
    decision: DecompositionDecision
    signals: DecompositionSignals
    code: ShapeReason = ShapeReason.POLICY_ENDORSED
    reason: str = ""
    #: What the scout could not establish, carried so a client can show it rather than
    #: being told a guess was a measurement.
    assumed: tuple[str, ...] = ()

    def to_json(self) -> JSONObject:
        """The four fields anything counting needs, plus the sentences people read.

        `policy_verdict` is `decompose` or `decline` rather than the policy's sentence:
        the sentence is next to it under `policy_explanation`, and a dashboard grouping
        runs by what the policy thought should not have to match prose to do it.
        """
        return {
            "execution_mode": self.mode.value,
            "executed_as": "hierarchical" if self.hierarchical else "direct",
            "reason_code": self.code.value,
            "reason": self.reason,
            "policy_verdict": "decompose" if self.decision.decompose else "decline",
            "policy_explanation": self.decision.explanation,
            "criteria_met": list(self.decision.reasons),
            "assumed_signals": list(self.assumed),
        }

    def as_decided(self, *, hierarchical: bool, code: ShapeReason, reason: str) -> RunShape:
        """The same run, once something later settled what it will actually do."""
        return replace(self, hierarchical=hierarchical, code=code, reason=reason)
