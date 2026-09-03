"""El contrato con el modelo planificador y su parseo.

El esquema esta fuera del texto del prompt para que lo que se pide y lo que
el parser exige sean una sola cosa y no dos que se separan con el tiempo.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from athena.planning.estados import PlanParseError
from athena.planning.grafo import TaskGraph
from athena.planning.nodos import PlanningLimits, TaskNode
from athena.subagents import DEFAULT_PROFILES, SubagentRole
from athena.types import JSONObject

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
