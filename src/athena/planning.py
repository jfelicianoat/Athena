"""Deciding whether a goal needs a plan, and validating the plan if it does.

`AgentLoop` executes. This layer decides whether execution needs to be broken up first, and
if so it holds the shape of that break-up as a graph the runtime can check. The two are
deliberately separate: the loop already works, and a planning layer that replaced it would
be betting the working part on the new part.

The first question is whether to plan at all, and the honest answer is usually no. A
`TaskGraph` for "fix the failing test in calc.py" adds bookkeeping, a second place for state
to be wrong, and a set of hand-offs between steps that were never independent — in exchange
for nothing, because there was only ever one thing to verify. `DecompositionPolicy` says no
by default and has to be argued into saying yes.

The second question is whether a proposed plan is any good, and that is not the model's call
either. A model asked for a graph will cheerfully produce one with a cycle, a dependency on
a task it forgot to include, two tasks with the same id, or eleven microtasks where two
would do. So the model proposes and the runtime disposes: every plan — model-written or
hand-written — goes through the same `TaskGraph.build`, and a plan that fails validation is
rejected whole rather than repaired into something nobody designed.

Nothing here imports a provider. Planning crosses `ModelProvider` like everything else, so a
deployment with no model at all can still build, validate and execute a graph it was handed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from athena.cancellation import CancellationToken
from athena.errors import AthenaRuntimeError
from athena.models import ModelMessage, ModelProvider, ModelRequest, ModelRole
from athena.subagents import DEFAULT_PROFILES, SubagentRole
from athena.types import JSONObject

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


#: What may follow what. A plan whose statuses can move arbitrarily is a plan whose state
#: cannot be trusted to mean anything, and "verified" is exactly the value worth protecting:
#: nothing reaches `COMPLETED` except from `RUNNING`.
_TRANSITIONS: Mapping[PlanStatus, frozenset[PlanStatus]] = {
    PlanStatus.PENDING: frozenset({PlanStatus.READY, PlanStatus.BLOCKED, PlanStatus.SKIPPED}),
    PlanStatus.READY: frozenset({PlanStatus.RUNNING, PlanStatus.BLOCKED, PlanStatus.SKIPPED}),
    PlanStatus.RUNNING: frozenset({PlanStatus.COMPLETED, PlanStatus.FAILED}),
    #: A completed task can be reopened only by replanning, which sends it back to PENDING.
    PlanStatus.COMPLETED: frozenset({PlanStatus.PENDING}),
    PlanStatus.FAILED: frozenset({PlanStatus.PENDING, PlanStatus.SKIPPED}),
    PlanStatus.BLOCKED: frozenset({PlanStatus.PENDING, PlanStatus.SKIPPED}),
    PlanStatus.SKIPPED: frozenset({PlanStatus.PENDING}),
}

_TERMINAL = frozenset({PlanStatus.COMPLETED, PlanStatus.SKIPPED})


# --------------------------------------------------------------------------- the model


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


class TaskGraph:
    """A validated plan. There is no way to hold an invalid one.

    Construction is the only entry point and it validates everything: ids, dependencies,
    acyclicity, limits, atomicity and redundancy. Mutation goes through `transition` and
    `replan_from`, both of which validate again. That is what "LLM output never bypasses
    graph validation" means in practice — there is no unchecked path to a graph object.
    """

    def __init__(self, nodes: Mapping[str, TaskNode], limits: PlanningLimits) -> None:
        # Private on purpose: `build` is the validating constructor.
        self._nodes = dict(nodes)
        self.limits = limits

    # -- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        nodes: Iterable[TaskNode],
        limits: PlanningLimits | None = None,
        *,
        collapse_single_children: bool = True,
    ) -> TaskGraph:
        """Validate a set of tasks into a graph, or refuse.

        Order matters. Ids are checked before dependencies, because "unknown dependency" is
        a confusing thing to be told when the real problem is that two tasks share a name.
        Cycles are checked before limits so the more specific failure wins.
        """
        bounds = limits or PlanningLimits()
        indexed = _index(nodes)
        _check_dependencies(indexed)
        _check_parents(indexed)
        _check_acyclic(indexed)
        if collapse_single_children:
            indexed = _collapse_single_children(indexed)
        _check_redundancy(indexed)
        _check_atomicity(indexed)
        _check_limits(indexed, bounds)
        return cls(indexed, bounds)

    # -- inspection --------------------------------------------------------

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._nodes

    @property
    def nodes(self) -> tuple[TaskNode, ...]:
        return tuple(self._nodes.values())

    def get(self, task_id: str) -> TaskNode:
        node = self._nodes.get(task_id)
        if node is None:
            raise UnknownDependencyError(f"No such task: {task_id}", details={"id": task_id})
        return node

    def roots(self) -> tuple[TaskNode, ...]:
        return tuple(node for node in self._nodes.values() if node.parent_id is None)

    def children_of(self, task_id: str) -> tuple[TaskNode, ...]:
        return tuple(node for node in self._nodes.values() if node.parent_id == task_id)

    def dependents_of(self, task_id: str) -> tuple[TaskNode, ...]:
        """Tasks that named this one as a dependency. One hop, not the closure."""
        return tuple(node for node in self._nodes.values() if task_id in node.dependencies)

    def depth_of(self, task_id: str) -> int:
        return _depth(self._nodes, task_id)

    def topological_order(self) -> tuple[TaskNode, ...]:
        """Dependency order. Acyclicity is a construction invariant, so this cannot fail."""
        return tuple(self._nodes[task_id] for task_id in _topological(self._nodes))

    def ready(self) -> tuple[TaskNode, ...]:
        """Tasks that could start now.

        Fan-out falls out of this rather than being a feature: several tasks depending on
        one completed task all become ready together, and a caller free to run them
        concurrently will. Fan-in is the same rule read backwards — a task with several
        dependencies appears only once all of them are done.
        """
        runnable: list[TaskNode] = []
        for node in self._nodes.values():
            if node.status not in (PlanStatus.PENDING, PlanStatus.READY):
                continue
            if all(
                self._nodes[dependency].status is PlanStatus.COMPLETED
                for dependency in node.dependencies
            ):
                runnable.append(node)
        return tuple(runnable)

    def is_complete(self) -> bool:
        return all(node.status in _TERMINAL for node in self._nodes.values())

    def total_attempts(self) -> int:
        return sum(node.attempts for node in self._nodes.values())

    def to_json(self) -> JSONObject:
        return {"tasks": [node.to_json() for node in self.topological_order()]}

    # -- mutation ----------------------------------------------------------

    def transition(
        self,
        task_id: str,
        status: PlanStatus,
        *,
        verification: JSONObject | None = None,
    ) -> TaskNode:
        """Move a task, or refuse to.

        An attempt is counted on entry to `RUNNING`, not on completion, because a task that
        crashed still spent one — counting only successes would let a failing task loop
        forever under a budget it never appears to touch.
        """
        node = self.get(task_id)
        if status is node.status:
            return node
        if status not in _TRANSITIONS[node.status]:
            raise InvalidTransitionError(
                f"A task cannot go from {node.status.value} to {status.value}",
                details={"id": task_id, "from": node.status.value, "to": status.value},
            )
        attempts = node.attempts + 1 if status is PlanStatus.RUNNING else node.attempts
        if attempts > self.limits.max_total_attempts:
            raise PlanLimitExceededError("The plan has spent its attempts", details={"id": task_id})
        updated = replace(
            node,
            status=status,
            attempts=attempts,
            verification=verification if verification is not None else node.verification,
        )
        self._nodes[task_id] = updated
        if status is PlanStatus.FAILED:
            self._block_dependents(task_id)
        return updated

    def _block_dependents(self, task_id: str) -> None:
        """A task whose dependency failed cannot start, and should say so.

        Transitively, because the second-order dependents are just as stuck and leaving them
        `PENDING` would have `ready()` quietly never return them with no explanation.
        """
        frontier = [task_id]
        while frontier:
            current = frontier.pop()
            for dependent in self.dependents_of(current):
                if dependent.status in (PlanStatus.PENDING, PlanStatus.READY):
                    self._nodes[dependent.id] = replace(dependent, status=PlanStatus.BLOCKED)
                    frontier.append(dependent.id)

    def affected_subgraph(self, task_id: str) -> tuple[str, ...]:
        """The failed task plus everything downstream of it, and nothing else.

        This is the whole point of planning as a graph rather than a list. When
        verification fails, what needs rethinking is the task that failed and the work that
        was going to consume its output — not the sibling that succeeded on another
        subsystem, and not the dependency that produced exactly what it promised.
        """
        seen = {task_id}
        frontier = [task_id]
        while frontier:
            current = frontier.pop()
            for dependent in self.dependents_of(current):
                if dependent.id not in seen:
                    seen.add(dependent.id)
                    frontier.append(dependent.id)
        for child in self.children_of(task_id):
            if child.id not in seen:
                seen.add(child.id)
                frontier.append(child.id)
                while frontier:
                    current = frontier.pop()
                    for grandchild in self.children_of(current):
                        if grandchild.id not in seen:
                            seen.add(grandchild.id)
                            frontier.append(grandchild.id)
        return tuple(node.id for node in self.topological_order() if node.id in seen)

    def replan_from(self, task_id: str, replacements: Iterable[TaskNode]) -> TaskGraph:
        """Rebuild only the affected subgraph, leaving proven work alone.

        The replacement set may only touch tasks in `affected_subgraph(task_id)`. Anything
        else is refused rather than merged, because a replan that quietly rewrites a
        completed task discards evidence that was already produced and paid for.
        """
        affected = set(self.affected_subgraph(task_id))
        incoming = _index(replacements)
        stray = set(incoming) - affected
        if stray:
            raise PlanningError(
                "A replan may only replace the affected subgraph",
                details={"unexpected": sorted(stray)},
            )
        survivors = {
            node_id: node for node_id, node in self._nodes.items() if node_id not in affected
        }
        # Replacements arrive unproven: carrying a status or a verification across from the
        # plan that failed would let a rewritten task inherit a result it never earned.
        fresh = {
            node_id: replace(node, status=PlanStatus.PENDING, verification=None)
            for node_id, node in incoming.items()
        }
        return TaskGraph.build({**survivors, **fresh}.values(), self.limits)


# --------------------------------------------------------------------------- validation


def _index(nodes: Iterable[TaskNode]) -> dict[str, TaskNode]:
    indexed: dict[str, TaskNode] = {}
    for node in nodes:
        if not node.id.strip():
            raise PlanningError("A task needs an id")
        if node.id in indexed:
            raise DuplicateTaskIdError(
                f"Two tasks share the id {node.id!r}", details={"id": node.id}
            )
        if node.id in node.dependencies:
            raise CyclicPlanError(f"Task {node.id!r} depends on itself", details={"id": node.id})
        indexed[node.id] = node
    if not indexed:
        raise PlanningError("A plan with no tasks is not a plan")
    return indexed


def _check_dependencies(nodes: Mapping[str, TaskNode]) -> None:
    for node in nodes.values():
        for dependency in node.dependencies:
            if dependency not in nodes:
                raise UnknownDependencyError(
                    f"Task {node.id!r} depends on {dependency!r}, which is not in the plan",
                    details={"id": node.id, "dependency": dependency},
                )


def _check_parents(nodes: Mapping[str, TaskNode]) -> None:
    for node in nodes.values():
        if node.parent_id is not None and node.parent_id not in nodes:
            raise UnknownDependencyError(
                f"Task {node.id!r} has parent {node.parent_id!r}, which is not in the plan",
                details={"id": node.id, "parent": node.parent_id},
            )
    for node_id in nodes:
        _depth(nodes, node_id)


def _check_acyclic(nodes: Mapping[str, TaskNode]) -> None:
    _topological(nodes)


def _topological(nodes: Mapping[str, TaskNode]) -> tuple[str, ...]:
    """Kahn's algorithm, which reports the cycle rather than merely detecting one.

    Naming the tasks still standing is the difference between a message a person can act on
    and one that sends them reading the whole plan.
    """
    remaining = {node_id: set(node.dependencies) for node_id, node in nodes.items()}
    ordered: list[str] = []
    while remaining:
        free = sorted(node_id for node_id, deps in remaining.items() if not deps)
        if not free:
            raise CyclicPlanError(
                "These tasks depend on each other in a loop",
                details={"tasks": sorted(remaining)},
            )
        for node_id in free:
            ordered.append(node_id)
            del remaining[node_id]
        for deps in remaining.values():
            deps.difference_update(free)
    return tuple(ordered)


def _depth(nodes: Mapping[str, TaskNode], task_id: str) -> int:
    depth = 0
    seen = {task_id}
    current = nodes[task_id].parent_id
    while current is not None:
        if current in seen:
            raise CyclicPlanError("A task is its own ancestor", details={"tasks": sorted(seen)})
        seen.add(current)
        depth += 1
        current = nodes[current].parent_id
    return depth


def _check_limits(nodes: Mapping[str, TaskNode], limits: PlanningLimits) -> None:
    if len(nodes) > limits.max_tasks:
        raise PlanLimitExceededError(
            f"A plan of {len(nodes)} tasks exceeds the limit of {limits.max_tasks}",
            details={"tasks": len(nodes), "max_tasks": limits.max_tasks},
        )
    children: dict[str, int] = {}
    for node in nodes.values():
        if node.parent_id is not None:
            children[node.parent_id] = children.get(node.parent_id, 0) + 1
        depth = _depth(nodes, node.id)
        if depth >= limits.max_depth:
            raise PlanLimitExceededError(
                f"Task {node.id!r} sits at depth {depth}, past the limit of {limits.max_depth}",
                details={"id": node.id, "depth": depth},
            )
    for parent_id, count in children.items():
        if count > limits.max_children:
            raise PlanLimitExceededError(
                f"Task {parent_id!r} has {count} children, past the limit of {limits.max_children}",
                details={"id": parent_id, "children": count},
            )


def _leaves(nodes: Mapping[str, TaskNode]) -> tuple[TaskNode, ...]:
    parents = {node.parent_id for node in nodes.values() if node.parent_id is not None}
    return tuple(node for node in nodes.values() if node.id not in parents)


def _check_atomicity(nodes: Mapping[str, TaskNode]) -> None:
    """Leaves are what gets executed, so leaves are what must be checkable.

    An interior node is a heading; it does not have to be atomic, and demanding that it be
    would make every plan one level deep.
    """
    for node in _leaves(nodes):
        missing = [
            name
            for name, value in (
                ("goal", node.goal.strip()),
                ("expected_output", node.expected_output.strip()),
            )
            if not value
        ]
        if not node.acceptance_criteria:
            missing.append("acceptance_criteria")
        if missing:
            raise NonAtomicTaskError(
                f"Task {node.id!r} cannot be executed or checked as written",
                details={"id": node.id, "missing": missing},
            )


def _check_redundancy(nodes: Mapping[str, TaskNode]) -> None:
    """Two siblings promising the same output are one task written twice.

    This is the mechanical half of "unnecessary microtasks". The other half — a step so
    small that naming it costs more than doing it — is not decidable from the text, and a
    guess at it would reject good plans as confidently as bad ones.
    """
    by_parent: dict[str | None, dict[str, str]] = {}
    for node in _leaves(nodes):
        promises = by_parent.setdefault(node.parent_id, {})
        signature = node.expected_output.strip().casefold()
        if not signature:
            continue
        first = promises.get(signature)
        if first is not None:
            raise RedundantTaskError(
                f"Tasks {first!r} and {node.id!r} promise the same output",
                details={"tasks": [first, node.id], "output": node.expected_output},
            )
        promises[signature] = node.id


def _collapse_single_children(nodes: Mapping[str, TaskNode]) -> dict[str, TaskNode]:
    """Fold a parent that has exactly one child into that child.

    A single child is a level of hierarchy that records no decision: nothing was divided,
    so there is nothing to conquer. Left in, these accumulate — each replan adds another —
    until the graph is mostly structure. Collapsing is safe because the child inherits the
    parent's place in the graph, so anything that depended on the parent still resolves.
    """
    result = dict(nodes)
    while True:
        children: dict[str, list[str]] = {}
        for node in result.values():
            if node.parent_id is not None:
                children.setdefault(node.parent_id, []).append(node.id)
        collapsible = next(
            (parent for parent, kids in sorted(children.items()) if len(kids) == 1), None
        )
        if collapsible is None:
            return result
        only_child = children[collapsible][0]
        parent = result[collapsible]
        child = result[only_child]
        merged = replace(
            child,
            parent_id=parent.parent_id,
            # The parent's dependencies were the subtree's dependencies; the child now
            # stands where the parent stood, so it must carry them.
            dependencies=tuple(dict.fromkeys(parent.dependencies + child.dependencies)),
            inputs=tuple(dict.fromkeys(parent.inputs + child.inputs)),
        )
        del result[collapsible]
        result[only_child] = merged
        result = {
            node_id: replace(
                node,
                parent_id=only_child if node.parent_id == collapsible else node.parent_id,
                dependencies=tuple(
                    only_child if dependency == collapsible else dependency
                    for dependency in node.dependencies
                ),
            )
            for node_id, node in result.items()
        }


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


# --------------------------------------------------------------------------- the planner


#: What the model is asked to return. Kept out of the prompt text so the shape it must
#: satisfy and the shape the parser enforces are one thing rather than two.
PLAN_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "goal", "expected_output", "acceptance_criteria"],
                "properties": {
                    "id": {"type": "string"},
                    "parent_id": {"type": ["string", "null"]},
                    "goal": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "expected_output": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                    "suggested_role": {
                        "type": "string",
                        "enum": [role.value for role in SubagentRole],
                    },
                    "toolsets": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

_PLANNER_INSTRUCTIONS = """\
You are decomposing one engineering objective into tasks that can be executed and checked \
independently.

Rules, all of which are enforced afterwards — a plan that breaks one is discarded whole:
- every task needs a concrete goal, a stated expected_output, and acceptance_criteria \
someone could check without asking you;
- dependencies must name tasks in this same plan, and must not form a loop;
- ids must be unique;
- do not split work that has one output; a task nobody can verify separately does not \
deserve to be a task;
- prefer few tasks. Depth costs more than breadth.

Return JSON only, matching the requested schema.\
"""


def parse_plan(document: str, *, limits: PlanningLimits | None = None) -> TaskGraph:
    """Turn a model's answer into a validated graph, or refuse it.

    Parsing and validating are one step from the caller's side on purpose: there is no
    intermediate "parsed but unchecked plan" object for anything to accidentally use.
    """
    try:
        payload: Any = json.loads(_strip_fences(document))
    except json.JSONDecodeError as exc:
        raise PlanParseError("The plan is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise PlanParseError("The plan is not a JSON object")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, str):
        raise PlanParseError("The plan has no task list")
    return TaskGraph.build((_node_from_json(entry) for entry in raw_tasks), limits)


def _strip_fences(document: str) -> str:
    text = document.strip()
    if not text.startswith("```"):
        return text
    without_open = text.split("\n", 1)[-1]
    return without_open.rsplit("```", 1)[0].strip()


def _node_from_json(entry: object) -> TaskNode:
    if not isinstance(entry, Mapping):
        raise PlanParseError("A task must be a JSON object")
    task_id = entry.get("id")
    goal = entry.get("goal")
    expected = entry.get("expected_output")
    for name, value in (("id", task_id), ("goal", goal), ("expected_output", expected)):
        if not isinstance(value, str) or not value.strip():
            raise PlanParseError(f"A task is missing {name}")
    parent = entry.get("parent_id")
    role_name = entry.get("suggested_role")
    try:
        role = SubagentRole(role_name) if isinstance(role_name, str) else SubagentRole.CODER
    except ValueError as exc:
        # An unrecognised role is refused rather than defaulted: silently turning an
        # invented specialism into "coder" would give a write-capable toolset to a task
        # the plan meant to be read-only.
        raise PlanParseError(f"Unknown role: {role_name!r}") from exc
    return TaskNode(
        id=str(task_id).strip(),
        goal=str(goal).strip(),
        expected_output=str(expected).strip(),
        parent_id=parent.strip() if isinstance(parent, str) and parent.strip() else None,
        inputs=_strings(entry.get("inputs")),
        acceptance_criteria=_strings(entry.get("acceptance_criteria")),
        dependencies=_strings(entry.get("dependencies")),
        suggested_role=role,
        toolsets=_strings(entry.get("toolsets")) or DEFAULT_PROFILES[role].toolsets,
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


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
    ) -> TaskGraph | None:
        """A validated graph, or `None` meaning "run this on the loop as it is".

        `None` is a real answer and the common one. Returning an empty graph instead would
        make every caller check for a special case that means the same thing.
        """
        decision = self.should_decompose(signals)
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


__all__ = [
    "PLAN_SCHEMA",
    "CyclicPlanError",
    "DecompositionDecision",
    "DecompositionPolicy",
    "DecompositionSignals",
    "DuplicateTaskIdError",
    "InvalidTransitionError",
    "NonAtomicTaskError",
    "PlanLimitExceededError",
    "PlanParseError",
    "PlanStatus",
    "Planner",
    "PlanningError",
    "PlanningLimits",
    "RedundantTaskError",
    "TaskGraph",
    "TaskNode",
    "UnknownDependencyError",
    "parse_plan",
]
