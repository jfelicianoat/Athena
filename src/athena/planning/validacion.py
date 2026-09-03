"""Invariantes del plan: dependencias, ciclos, limites, atomicidad y redundancia.

Un plan que no cumple estas reglas no se ejecuta: se rechaza al construirse,
que es mucho mas barato que descubrirlo a mitad de un run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from athena.planning.estados import (
    CyclicPlanError,
    DuplicateTaskIdError,
    NonAtomicTaskError,
    PlanLimitExceededError,
    PlanningError,
    RedundantTaskError,
    UnknownDependencyError,
)
from athena.planning.nodos import PlanningLimits, TaskNode


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
