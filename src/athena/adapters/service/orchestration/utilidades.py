"""Conversiones sueltas: final de un grafo, objetivo unico y presupuestos."""

from __future__ import annotations

from dataclasses import replace

from athena.adapters.service.orchestration.forma import _AS_STEP, RunShape
from athena.diagnosis import diagnose_result, inconclusive_reason
from athena.errors import (
    VerificationInconclusive,
)
from athena.graph_executor import GraphResult
from athena.planning import (
    DecompositionDecision,
    PlanStatus,
    TaskGraph,
    TaskNode,
)
from athena.subagents import (
    DEFAULT_PROFILES,
    SubagentProfile,
    SubagentRole,
)
from athena.types import JSONValue
from athena.working_state import PlanStep, StepStatus, WorkingState


def _ending(result: GraphResult) -> dict[str, JSONValue]:
    """Por que no termino bien, en los terminos de quien tendria que hacer algo.

    Tres finales distintos que antes se contaban como uno: una tarea que fallo, un plan
    que termino entero y no se pudo dar por bueno, y un plan que no llego al final. Decir
    «the plan did not finish» del segundo mandaba a mirar unas tareas que estaban todas
    completadas — la peor pista posible, porque parece informacion.
    """
    failed = [item for item in result.evidence if not item.succeeded]
    if failed:
        return {
            "error_code": next(
                (item.error_code for item in failed if item.error_code), "graph_incomplete"
            ),
            "message": failed[0].summary,
        }
    goal = result.goal_verification
    if goal is not None and not goal.permits_completion:
        razon = inconclusive_reason(diagnose_result(goal))
        if razon is None:
            return {"error_code": "verification_failure", "message": goal.summary}
        return {
            "error_code": VerificationInconclusive.code,
            "message": goal.summary,
            "reason": razon.value,
        }
    return {"error_code": "graph_incomplete", "message": "The plan did not finish"}


def _whole_goal(objective: str) -> TaskGraph:
    """The goal as a plan of one task, for a run that must go through the graph.

    Its acceptance criterion is the project's own checks, which is the same thing the
    executor verifies at the end. Inventing a narrower criterion would let the task report
    success against a bar nobody set.
    """
    return TaskGraph.build(
        [
            TaskNode(
                id="whole",
                goal=objective,
                expected_output="The objective, carried out in the workspace.",
                acceptance_criteria=("The project's own verification commands pass.",),
                suggested_role=SubagentRole.CODER,
                toolsets=DEFAULT_PROFILES[SubagentRole.CODER].toolsets,
            )
        ]
    )


def budgeted(profile: SubagentProfile, seconds: float | None) -> SubagentProfile:
    """Dar a un delegado el reloj del despliegue, sin tocar sus otros límites.

    Sólo el reloj. Las iteraciones y las llamadas a herramienta acotan cuánto *hace* un
    delegado, y eso no cambia porque el modelo sea lento; el tiempo sí.

    Público porque hay dos caminos que delegan —el plan y el run monoagente que pide un
    especialista— y durante un tiempo sólo el primero aplicó esto. El segundo dejaba al
    explorer con sus 300 s de fábrica mientras el mismo despliegue permitía 900 s para
    **una sola** llamada al modelo: un delegado cuyo presupuesto entero es más corto que
    la llamada que lo ocupa no termina nunca su primer turno. Es la misma regla que
    `athena_service` ya verifica al arrancar entre `ATHENA_TASK_TIMEOUT_SECONDS` y
    `ATHENA_MODEL_WAIT_SECONDS`, aplicada donde faltaba.
    """
    if seconds is None:
        return profile
    return replace(profile, budget=replace(profile.budget, timeout_seconds=seconds))


def _first_goal(graph: TaskGraph) -> str:
    """Con qué nombrar un plan cuyo objetivo no se guardó.

    Un plan viejo puede no traerlo. El objetivo de su primera tarea describe mal el
    conjunto, pero describe algo real; inventar una frase sería peor.
    """
    nodes = graph.topological_order()
    return nodes[0].goal if nodes else "Plan sin objetivo registrado"


def _settled(shape: RunShape) -> DecompositionDecision:
    """La decisión ya tomada, en los términos en que el planificador la entiende.

    Cuando el cliente pidió el plan que la política no habría propuesto, la razón es ésa y
    se dice: la explicación viaja al informe del run, y llamarla "criterios cumplidos"
    sería atribuir a la evidencia una decisión que no tomó.
    """
    if shape.decision.decompose == shape.hierarchical:
        return shape.decision
    return DecompositionDecision(
        shape.hierarchical,
        shape.decision.reasons,
        "Requested by the caller rather than argued for by the evidence.",
    )


def _with_plan(working: WorkingState, graph: TaskGraph) -> WorkingState:
    """Put the plan where a reconnecting client looks for it.

    The snapshot is what a client resynchronises against, so a plan that lives only in the
    event stream disappears the moment somebody closes a laptop lid. Dependency order,
    because that is the order the work happens in and any other order invites a reader to
    infer a sequence that is not there.
    """
    nodes = graph.topological_order()
    steps = tuple(
        PlanStep(node.goal, _AS_STEP.get(node.status, StepStatus.PENDING), task_id=node.id)
        for node in nodes
    )
    if not steps:
        return working
    running = next(
        (index for index, node in enumerate(nodes) if node.status is PlanStatus.RUNNING), None
    )
    return working.with_plan(steps, running)
