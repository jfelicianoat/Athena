"""Planificacion: decidir si descomponer, pedir el plan y validarlo.

Partido por seccion:

- `estados`         — errores y estados de tarea, con su tabla de transiciones.
- `validacion`      — invariantes que un plan tiene que cumplir.
- `grafo`           — nodos, limites y el grafo de tareas.
- `descomposicion`  — cuando merece la pena partir un objetivo.
- `esquema`         — el contrato con el modelo y su parseo.
- `planificador`    — el planificador propiamente dicho.
"""

from __future__ import annotations

from athena.planning.descomposicion import (
    DecompositionDecision,
    DecompositionPolicy,
    DecompositionSignals,
    PlanBoard,
    describe_plan,
)
from athena.planning.esquema import PLAN_SCHEMA, parse_plan
from athena.planning.estados import (
    CyclicPlanError,
    DuplicateTaskIdError,
    InvalidTransitionError,
    NonAtomicTaskError,
    PlanLimitExceededError,
    PlanningError,
    PlanParseError,
    PlanStatus,
    RedundantTaskError,
    UnknownDependencyError,
)
from athena.planning.grafo import TaskGraph
from athena.planning.nodos import PlanningLimits, TaskNode
from athena.planning.planificador import Planner

__all__ = [
    "PLAN_SCHEMA",
    "CyclicPlanError",
    "DecompositionDecision",
    "DecompositionPolicy",
    "DecompositionSignals",
    "DuplicateTaskIdError",
    "InvalidTransitionError",
    "NonAtomicTaskError",
    "PlanBoard",
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
    "describe_plan",
    "parse_plan",
]
