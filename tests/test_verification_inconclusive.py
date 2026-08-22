"""No haber podido comprobar no es lo mismo que haber comprobado que está mal.

Athena lo tenía escrito —`InconclusiveReason` existía, con su docstring diciendo
exactamente esto— y no lo usaba nadie. Mientras tanto, un run cuyos checks no se podían
ejecutar terminaba con `error_code: verification_failure`, que es echarle la culpa al
cambio de una máquina rota o de un proyecto que nunca definió checks.

Estas pruebas fijan la distinción en los tres sitios por donde sale: el error tipado, la
política de recuperación y lo que se publica.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.diagnosis import (
    FailureDiagnosis,
    FailureKind,
    InconclusiveReason,
    inconclusive_reason,
)
from athena.errors import VerificationFailure, VerificationInconclusive
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.graph_executor import GraphResult
from athena.models import ModelResponse
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.recovery import RecoveryAction, RecoveryPolicy
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.verification import (
    CommandVerificationPolicy,
    VerificationEvidence,
    VerificationPlanner,
    VerificationResult,
    VerificationStatus,
)
from athena.workspace import Workspace


def _diagnostico(kind: FailureKind) -> FailureDiagnosis:
    return FailureDiagnosis(kind=kind, summary="da igual", guidance="da igual")


def _loop(root: Path) -> tuple[AgentLoop, Workspace, list[RuntimeEvent]]:
    bus = InMemoryEventBus()
    eventos: list[RuntimeEvent] = []
    bus.subscribe(eventos.append)
    workspace = Workspace.from_path(root)
    registry = ToolRegistry(repository_read_tools())
    loop = AgentLoop(
        FakeModelProvider([ModelResponse("Ya está.", "scripted", "stop")]),
        registry,
        ToolExecutor(
            registry,
            PolicyPermissionEngine(PermissionPolicy()),
            InMemoryToolResultStore(),
            bus,
        ),
        ContextBuilder(workspace),
        bus,
        verification=CommandVerificationPolicy(VerificationPlanner(workspace), event_bus=bus),
        config=AgentLoopConfig(max_iterations=3, session_timeout_seconds=60.0),
    )
    return loop, workspace, eventos


# -- el error tipado ---------------------------------------------------------


def test_no_verificado_no_hereda_de_verificacion_fallida() -> None:
    """Si heredase, la política de recuperación las trataría igual sin querer.

    Un fallo de verificación se responde devolviendo evidencia para que alguien arregle el
    cambio. Aquí no hay evidencia que devolver, y gastar ciclos de reparación sobre una
    máquina rota es cómo un run se queda sin presupuesto pareciendo ocupado.
    """
    assert not issubclass(VerificationInconclusive, VerificationFailure)
    assert not issubclass(VerificationFailure, VerificationInconclusive)
    assert VerificationInconclusive.code != VerificationFailure.code


def test_la_recuperacion_de_no_verificado_es_parar_y_no_reparar() -> None:
    politica = RecoveryPolicy()

    sin_evidencia = politica.decide(VerificationInconclusive("no se pudo comprobar"))
    con_evidencia = politica.decide(VerificationFailure("el test falla"))

    assert sin_evidencia.action is RecoveryAction.STOP
    assert con_evidencia.action is RecoveryAction.RETURN_EVIDENCE, (
        "el camino bueno tiene que seguir intacto: un fallo real sí merece reparación"
    )


# -- lo que sale del bucle ---------------------------------------------------


def test_un_proyecto_sin_checks_no_hace_fallar_al_cambio(tmp_path: Path) -> None:
    """El run no completa —sin evidencia no hay finalización— pero dice por qué."""
    (tmp_path / "notas.txt").write_text("nada que verificar", encoding="utf-8")

    async def scenario() -> None:
        loop, workspace, eventos = _loop(tmp_path)

        resultado = await loop.run("Di que has terminado", workspace, CancellationSource().token)

        assert resultado.status is AgentRunStatus.FAILED
        assert isinstance(resultado.error, VerificationInconclusive)
        assert resultado.error.details["reason"] == InconclusiveReason.NO_CHECKS_DEFINED.value

        (fallo,) = [e for e in eventos if e.name is EventName.AGENT_FAILED]
        assert fallo.payload["error_code"] == "verification_inconclusive"
        # La razón viaja como dato, no dentro de una frase: nada que cuente lee frases.
        assert fallo.payload["reason"] == "no_checks_defined"

    asyncio.run(scenario())


def test_el_evento_de_verificacion_dice_por_que_no_pudo_concluir(tmp_path: Path) -> None:
    """«Inconclusive» a secas se lee como un problema de configuración, y casi nunca lo es."""
    (tmp_path / "notas.txt").write_text("nada que verificar", encoding="utf-8")

    async def scenario() -> None:
        loop, workspace, eventos = _loop(tmp_path)
        await loop.run("Di que has terminado", workspace, CancellationSource().token)

        (verificacion,) = [e for e in eventos if e.name is EventName.VERIFICATION_COMPLETED]
        assert verificacion.payload["status"] == "inconclusive"
        assert verificacion.payload["inconclusive_reason"] == "no_checks_defined"

    asyncio.run(scenario())


def test_un_fallo_de_codigo_no_lleva_razon_de_inconcluso() -> None:
    """Un cambio roto sí se comprobó: hay evidencia, y dice que está mal."""
    assert inconclusive_reason(_diagnostico(FailureKind.CODE_ERROR)) is None


# -- el mapa completo --------------------------------------------------------


def test_cada_diagnostico_cae_de_un_lado_o_del_otro_a_proposito() -> None:
    """Que un tipo de fallo cambie de lado tiene que costar cambiar esta tabla.

    Es la decisión entera de la fase en cinco líneas: a quién se culpa cuando algo no
    pasa. Dejarla implícita en el código la haría cambiar sin que nadie se enterase.
    """
    esperado = {
        FailureKind.CODE_ERROR: None,
        FailureKind.TEST_ERROR: None,
        FailureKind.PREEXISTING_FAILURE: None,
        FailureKind.UNKNOWN: None,
        FailureKind.DEPENDENCY_ERROR: InconclusiveReason.DEPENDENCY_MISSING,
        FailureKind.ENVIRONMENT_ERROR: InconclusiveReason.ENVIRONMENT_INCOMPLETE,
        FailureKind.TOOL_FAILURE: InconclusiveReason.TOOL_UNAVAILABLE,
        FailureKind.INSUFFICIENT_EVIDENCE: InconclusiveReason.NO_CHECKS_DEFINED,
    }

    obtenido = {kind: inconclusive_reason(_diagnostico(kind)) for kind in FailureKind}

    assert obtenido == esperado
    assert set(esperado) == set(FailureKind), "un FailureKind nuevo entró sin decidir de qué lado"


# -- el camino jerarquico ----------------------------------------------------


def _grafo_terminado(goal: VerificationResult | None) -> GraphResult:
    from athena.graph_executor import TaskEvidence
    from athena.planning import TaskGraph, TaskNode
    from athena.state import ExecutionOutcome
    from athena.subagents import SubagentRole

    grafo = TaskGraph.build(
        [
            TaskNode(
                id="unica",
                goal="Arreglar add()",
                expected_output="add() suma",
                acceptance_criteria=("los tests pasan",),
            )
        ]
    )
    return GraphResult(
        outcome=ExecutionOutcome.FAILED,
        graph=grafo,
        evidence=(
            TaskEvidence(
                task_id="unica",
                role=SubagentRole.CODER,
                outcome=ExecutionOutcome.COMPLETED,
                summary="Cambiado el operador.",
            ),
        ),
        goal_verification=goal,
    )


def test_un_plan_que_termino_entero_no_se_reporta_como_plan_sin_terminar() -> None:
    """Decirlo mandaba a mirar unas tareas que estaban todas completadas.

    Es peor que no decir nada, porque parece información: quien la siga se pasará el rato
    buscando el fallo en la única parte del run que sí funcionó.
    """
    from athena.adapters.service.orchestration import _ending

    final = _ending(
        _grafo_terminado(
            VerificationResult(
                VerificationStatus.INCONCLUSIVE,
                (VerificationEvidence(kind="plan", summary="no hay checks"),),
                "Verification is inconclusive: the project defines no checks Athena may run.",
            )
        )
    )

    assert final["error_code"] == "verification_inconclusive"
    assert final["reason"] == "no_checks_defined"
    assert "did not finish" not in str(final["message"])


def test_una_tarea_que_fallo_sigue_contandose_como_lo_que_es() -> None:
    """El caso bueno no se toca: si algo falló, el fallo manda y lo dice la tarea."""
    from athena.adapters.service.orchestration import _ending
    from athena.graph_executor import GraphResult, TaskEvidence
    from athena.planning import TaskGraph, TaskNode
    from athena.state import ExecutionOutcome
    from athena.subagents import SubagentRole

    resultado = GraphResult(
        outcome=ExecutionOutcome.FAILED,
        graph=TaskGraph.build(
            [
                TaskNode(
                    id="unica",
                    goal="x",
                    expected_output="y",
                    acceptance_criteria=("z",),
                )
            ]
        ),
        evidence=(
            TaskEvidence(
                task_id="unica",
                role=SubagentRole.CODER,
                outcome=ExecutionOutcome.FAILED,
                summary="El subagente se quedó sin presupuesto.",
                error_code="budget_exceeded",
            ),
        ),
    )

    final = _ending(resultado)

    assert final["error_code"] == "budget_exceeded"
    assert "reason" not in final
