"""El planificador: pide un plan al modelo y lo devuelve ya validado."""

from __future__ import annotations

from athena.cancellation import CancellationToken
from athena.models import ModelMessage, ModelProvider, ModelRequest, ModelRole
from athena.planning.descomposicion import (
    DecompositionDecision,
    DecompositionPolicy,
    DecompositionSignals,
)
from athena.planning.esquema import _PLANNER_INSTRUCTIONS, PLAN_SCHEMA, parse_plan
from athena.planning.grafo import TaskGraph
from athena.planning.nodos import PlanningLimits


class Planner:
    """Asks a model for a plan and refuses to believe it without checking.

    Holds a `ModelProvider`, which is a port. It never learns which provider it has, and a
    deployment with none can still use everything above this class.
    """

    def __init__(
        self,
        provider: ModelProvider,
        *,
        policy: DecompositionPolicy | None = None,
        limits: PlanningLimits | None = None,
    ) -> None:
        self.provider = provider
        self.policy = policy or DecompositionPolicy()
        self.limits = limits or PlanningLimits()

    def should_decompose(self, signals: DecompositionSignals) -> DecompositionDecision:
        """The first question, answered without spending a model call on it."""
        return self.policy.assess(signals)

    async def plan(
        self,
        objective: str,
        signals: DecompositionSignals,
        cancellation: CancellationToken,
        *,
        decided: DecompositionDecision | None = None,
    ) -> TaskGraph | None:
        """A validated graph, or `None` meaning "run this on the loop as it is".

        `None` is a real answer and the common one. Returning an empty graph instead would
        make every caller check for a special case that means the same thing.

        `decided` is for callers that have already put this question to the policy — the
        service asks before it starts a run, so that it can tell a client which shape it is
        getting. Asking twice is not a safety measure: a caller that weighed the answer
        against something the policy cannot see, such as an explicit request, would have
        its conclusion silently reversed here, and the run would come out monoagent while
        everything upstream said otherwise.
        """
        decision = decided if decided is not None else self.should_decompose(signals)
        if not decision.decompose:
            return None
        cancellation.raise_if_cancelled()
        request = ModelRequest(
            messages=(
                ModelMessage(ModelRole.SYSTEM, _PLANNER_INSTRUCTIONS),
                ModelMessage(ModelRole.USER, _brief(objective, signals, self.limits)),
            ),
            response_schema=PLAN_SCHEMA,
        )
        response = await self.provider.complete(request, cancellation)
        return parse_plan(response.content, limits=self.limits)


def _brief(objective: str, signals: DecompositionSignals, limits: PlanningLimits) -> str:
    """The limits go to the model as well as being enforced.

    Enforcement alone produces a rejected plan and a wasted call; telling it first usually
    produces an acceptable one. Neither replaces the other — the model is informed, not
    trusted.
    """
    reasons = signals.met()
    return (
        f"Objective:\n{objective}\n\n"
        f"This was judged worth decomposing because: {'; '.join(reasons) or 'unstated'}.\n\n"
        f"Hard limits: at most {limits.max_tasks} tasks, at most {limits.max_children} "
        f"children per task, and no deeper than {limits.max_depth} levels."
    )
