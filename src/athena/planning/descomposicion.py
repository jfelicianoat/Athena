"""Cuando merece la pena descomponer, y como se cuenta el plan resultante.

Descomponer tiene coste: la politica exige senales concretas antes de partir
un objetivo, en vez de partirlo siempre por si acaso.
"""

from __future__ import annotations

from dataclasses import dataclass

from athena.planning.estados import PlanStatus
from athena.planning.grafo import TaskGraph


def _has_independent_pair(graph: TaskGraph) -> bool:
    """Whether any two tasks could be in flight at once.

    Transitive, not immediate: a task depending on a task that depends on a third waits
    for the third too, and a check that only read the direct dependencies would call that
    chain concurrent.
    """
    reachable: dict[str, set[str]] = {}
    for node in graph.topological_order():
        inherited: set[str] = set()
        for dependency in node.dependencies:
            inherited.add(dependency)
            inherited |= reachable.get(dependency, set())
        reachable[node.id] = inherited
    ids = list(reachable)
    return any(
        other not in reachable[one] and one not in reachable[other]
        for index, one in enumerate(ids)
        for other in ids[index + 1 :]
    )


class PlanBoard:
    """Which run is executing which plan, for anything that needs to show one.

    A deliberately dumb shared place rather than a reference from the executor to a
    channel or from a channel to the executor. Both of those would make one of them know
    about the other, and neither has any business doing so: the executor's job is to run
    a graph, and a channel's is to describe one.

    Bounded, because a board that remembered every plan ever run would be a slow leak in
    a process that is meant to stay up.
    """

    def __init__(self, capacity: int = 32) -> None:
        self._plans: dict[str, TaskGraph] = {}
        self.capacity = max(1, capacity)

    def record(self, run_id: str, graph: TaskGraph) -> None:
        self._plans[run_id] = graph
        while len(self._plans) > self.capacity:
            self._plans.pop(next(iter(self._plans)))

    def plan_for(self, run_id: str) -> TaskGraph | None:
        return self._plans.get(run_id)

    def forget(self, run_id: str) -> None:
        self._plans.pop(run_id, None)


def describe_plan(graph: TaskGraph) -> str:
    """A plan as a person would read it in a chat window.

    Indented by dependency depth, because a plan rendered as a flat list does not say
    what was waiting on what — which is the only thing that makes it a plan rather than a
    queue.
    """
    marks = {
        PlanStatus.COMPLETED: "✓",
        PlanStatus.RUNNING: "▶",
        PlanStatus.FAILED: "✕",
        PlanStatus.BLOCKED: "⊘",
        PlanStatus.SKIPPED: "·",
    }
    lines: list[str] = []
    depth: dict[str, int] = {}
    for node in graph.topological_order():
        level = (
            0
            if not node.dependencies
            else 1 + max(depth.get(dependency, 0) for dependency in node.dependencies)
        )
        depth[node.id] = level
        mark = marks.get(node.status, "○")
        role = f" [{node.suggested_role.value}]" if node.suggested_role else ""
        lines.append(f"{'  ' * level}{mark} {node.id}{role} — {node.goal}")
    done = sum(1 for node in graph.nodes if node.status is PlanStatus.COMPLETED)
    header = f"Plan: {done} de {len(graph)} tareas hechas"
    return header + "\n" + "\n".join(lines)


# --------------------------------------------------------------- should we plan at all


@dataclass(frozen=True, slots=True)
class DecompositionSignals:
    """Evidence about a goal, in the terms the decision is actually made in.

    Deliberately not "how hard does this feel". Every field is something a caller can
    establish — by asking a model, by reading a repository, or by knowing what it just
    asked for — and the policy below turns them into an answer the same way every time.
    """

    independently_verifiable_outputs: int = 1
    #: Whether any output genuinely has to wait for another. Two things done in sequence by
    #: habit are not a dependency.
    has_meaningful_dependencies: bool = False
    parallelisable_investigation: bool = False
    high_implementation_risk: bool = False
    subsystems_touched: int = 1
    distinct_roles_required: int = 1

    def met(self) -> tuple[str, ...]:
        """Which of the six criteria this goal actually meets."""
        criteria = (
            (
                "multiple independently verifiable outputs",
                self.independently_verifiable_outputs > 1,
            ),
            ("meaningful dependencies", self.has_meaningful_dependencies),
            ("parallelisable investigation", self.parallelisable_investigation),
            ("high implementation risk", self.high_implementation_risk),
            ("multiple files or subsystems", self.subsystems_touched > 1),
            ("different specialist roles", self.distinct_roles_required > 1),
        )
        return tuple(name for name, holds in criteria if holds)


@dataclass(frozen=True, slots=True)
class DecompositionDecision:
    decompose: bool
    reasons: tuple[str, ...]
    explanation: str


@dataclass(frozen=True, slots=True)
class DecompositionPolicy:
    """Says no unless the evidence argues otherwise.

    Two gates, and both must pass. `minimum_criteria` stops a single weak signal from
    producing a graph. The verifiable-outputs gate is the one that matters: if there is only
    one thing to check at the end, a graph adds hand-offs between steps that were never
    independent and a second place for state to be wrong, and buys nothing — the loop
    already knows how to work through one objective.
    """

    minimum_criteria: int = 2
    require_multiple_outputs: bool = True

    def assess(self, signals: DecompositionSignals) -> DecompositionDecision:
        reasons = signals.met()
        if self.require_multiple_outputs and signals.independently_verifiable_outputs < 2:
            return DecompositionDecision(
                False,
                reasons,
                "One verifiable output, so a graph would add bookkeeping and no assurance. "
                "The AgentLoop handles this directly.",
            )
        if len(reasons) < self.minimum_criteria:
            return DecompositionDecision(
                False,
                reasons,
                f"Only {len(reasons)} of the decomposition criteria hold. "
                "The AgentLoop handles this directly.",
            )
        return DecompositionDecision(
            True, reasons, "Decomposition is worth its overhead here: " + "; ".join(reasons)
        )

    def assess_plan(self, graph: TaskGraph) -> DecompositionDecision:
        """Whether *this* plan earns the overhead, now that there is one to look at.

        A separate question from `assess`, and asked later. `assess` weighs evidence about
        a goal before anybody has decomposed it; this weighs the decomposition that came
        back. A model asked to divide work will divide it, and the result can be a list of
        steps rather than a graph — which the loop already knows how to work through, one
        after another, without the hand-offs.

        Structural, and not a count. "More than one task" is not the signal: five tasks in
        a chain, all for the same specialist, is a to-do list. What a graph actually buys
        is two things the loop cannot do —

        - **concurrency**: two tasks neither of which waits for the other really do run at
          the same time, and that is wall-clock a loop cannot recover;
        - **specialisation**: tasks with different roles carry different authority, and an
          explorer that cannot write is a guarantee, not a hint.

        — so a plan offering neither is executed directly, whatever its shape. Validity is
        `TaskGraph.build`'s question and it has already answered it: this one is only about
        worth.
        """
        benefits: list[str] = []
        if _has_independent_pair(graph):
            benefits.append("tasks that can run at the same time")
        if len({node.suggested_role for node in graph.nodes}) > 1:
            benefits.append("work for more than one specialist")
        if not benefits:
            return DecompositionDecision(
                False,
                (),
                f"The plan holds {len(graph)} task(s) in one sequence for one specialist, "
                "so a graph would add hand-offs and buy nothing the loop does not already "
                "do.",
            )
        return DecompositionDecision(
            True,
            tuple(benefits),
            "This plan is worth executing as a graph: " + "; ".join(benefits),
        )


# --------------------------------------------------------------------------- the planner


#: What the model is asked to return. Kept out of the prompt text so the shape it must
