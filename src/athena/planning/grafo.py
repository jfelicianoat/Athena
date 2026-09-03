"""El grafo de tareas: nodos, limites y transiciones.

El grafo es inmutable hacia fuera salvo por `transition`: quien quiera otro
plan pide `replan_from`, que devuelve un grafo nuevo.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from athena.planning.estados import (
    _TERMINAL,
    _TRANSITIONS,
    InvalidTransitionError,
    PlanLimitExceededError,
    PlanningError,
    PlanStatus,
    UnknownDependencyError,
)
from athena.planning.nodos import PlanningLimits, TaskNode
from athena.planning.validacion import (
    _check_acyclic,
    _check_atomicity,
    _check_dependencies,
    _check_limits,
    _check_parents,
    _check_redundancy,
    _collapse_single_children,
    _depth,
    _index,
    _topological,
)
from athena.types import JSONObject


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

    def mark_interrupted(self) -> tuple[str, ...]:
        """After a restart: whatever was running is now of unknown outcome.

        Deliberately mirrors `SessionStore.mark_interrupted` and `TaskManager`'s. A task
        the runtime stopped watching did not necessarily fail and certainly did not
        necessarily succeed — and re-running it blindly could repeat a side effect nobody
        asked for twice.
        """
        interrupted: list[str] = []
        for node_id, node in self._nodes.items():
            if node.status is PlanStatus.RUNNING:
                self._nodes[node_id] = replace(node, status=PlanStatus.RECOVERY_PENDING)
                interrupted.append(node_id)
        return tuple(interrupted)

    def needs_recovery(self) -> tuple[TaskNode, ...]:
        """Tasks waiting for somebody to say what happened to them."""
        return tuple(
            node for node in self._nodes.values() if node.status is PlanStatus.RECOVERY_PENDING
        )

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
