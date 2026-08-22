"""Los diez escenarios de aceptación de la integración DeepSeek, ejercitados de punta a punta.

**Los diez no vienen del prompt maestro.** Esa lista no está en el repositorio, así que se
derivan aquí de lo que las diecisiete fases construyeron de verdad: uno por propiedad que
alguien podría creerse mal si dejara de cumplirse. Decirlo importa — presentarlos como «los
escenarios que se pidieron» sería atribuirles una autoridad que no tienen.

Cada escenario defiende una frase que, si se rompe, hace que Athena **mienta** en vez de
fallar. Ese es el criterio de selección y no la cobertura: un runtime que se cae se arregla,
y uno que informa mal de lo que hizo se arregla cuando alguien se da cuenta.

Corren con proveedores guionizados para que pasen en cada suite. Los que además se
comprobaron contra el broker real durante su fase lo dicen en su docstring, porque «probado
con un modelo de mentira» y «probado con uno de verdad» no son la misma afirmación.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from athena.adapters.service.orchestration import OrchestrationSettings, Orchestrator
from athena.cancellation import CancellationSource, CancellationToken
from athena.checkpoints import CheckpointStore
from athena.errors import (
    ModelPermanentError,
    ToolContractError,
    VerificationFailure,
    VerificationInconclusive,
)
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.goals import GoalBoard
from athena.hooks import HookContext, HookEvent, HookRegistry
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.profiles import DOCUMENTS, SOFTWARE_ENGINEERING, Evidence, ProfileRegistry
from athena.project_memory import SqliteProjectMemory, render_for_context
from athena.provider_router import ProviderEntry, ProviderRegistry, ProviderRouter
from athena.recovery import RecoveryAction, RecoveryPolicy
from athena.rollback import RollbackLedger, RollbackScope, checkpointing_hook
from athena.run_event_log import RunEventLog, replay
from athena.session_store import SqliteSessionStore
from athena.stores import SqliteToolResultStore
from athena.subagents import SubagentBudget, SubagentRole
from athena.types import JSONObject
from athena.verification import (
    ArtifactVerificationPolicy,
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)
from athena.workspace import Workspace


class _Guionizado(ModelProvider):
    def __init__(
        self,
        respuestas: Sequence[ModelResponse] = (),
        *,
        falla: Exception | None = None,
        structured: bool = True,
    ) -> None:
        self._respuestas = list(respuestas)
        self._falla = falla
        self._structured = structured
        self.llamadas = 0

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        cancellation.raise_if_cancelled()
        self.llamadas += 1
        if self._falla is not None:
            raise self._falla
        if self._respuestas:
            return self._respuestas.pop(0)
        return ModelResponse("Listo.", "guion", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:  # pragma: no cover
            yield ModelEvent(EventName.MODEL_COMPLETED, "nunca")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, True, self._structured)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


def _evento(
    name: EventName,
    session: str,
    payload: JSONObject | None = None,
    corr: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(name, session, payload or {}, corr)


# --------------------------------------------------------------- escenario 1


def test_1_un_respaldo_que_no_cumple_no_es_un_respaldo() -> None:
    """Fase 10. Caer a un proveedor que no da la garantía pedida es degradar con otro nombre.

    El run continuaría sin lo que alguien pidió y fallaría más tarde, atribuido al sitio
    equivocado. Verificado contra el broker durante su fase.
    """

    async def escenario() -> None:
        primario = _Guionizado(falla=ModelPermanentError("el primario no esta"))
        incapaz = _Guionizado(structured=False)
        router = ProviderRouter(
            ProviderRegistry(
                ProviderEntry("primario", primario),
                [ProviderEntry("respaldo", incapaz)],
            )
        )

        with pytest.raises(ModelPermanentError):
            await router.complete(
                ModelRequest(messages=(), response_schema={"type": "object"}),
                CancellationSource().token,
            )

        assert incapaz.llamadas == 0, "se uso un respaldo que no daba lo requerido"

    asyncio.run(escenario())


# --------------------------------------------------------------- escenario 2


def test_2_lo_que_hizo_un_run_sobrevive_al_proceso_con_su_autor(tmp_path: Path) -> None:
    """Fase 6. Un run con delegados tiene una historia, no una por agente.

    Verificado contra el broker: 39 hechos de un run jerárquico, 28 de su delegado, leídos
    por un servicio que no vio el run ocurrir.
    """

    async def escenario() -> None:
        log = RunEventLog(tmp_path / "events.db")
        await log.record(_evento(EventName.AGENT_STARTED, "run-1"))
        await log.record(
            _evento(
                EventName.SUBAGENT_STARTED,
                "run-1",
                {"role": "explorer", "session_id": "hijo"},
                "hijo",
            )
        )
        await log.record(_evento(EventName.TOOL_COMPLETED, "hijo", {"tool": "grep"}))
        await log.record(_evento(EventName.AGENT_COMPLETED, "run-1"))

        # Un proceso nuevo sobre el mismo fichero: nadie vio pasar nada de esto.
        otra_vida = RunEventLog(tmp_path / "events.db")
        historia = await otra_vida.read("run-1")

        assert [item.name for item in historia].count("tool.completed") == 1
        delegado = next(item for item in historia if item.name == "tool.completed")
        assert delegado.actor == "explorer"
        assert replay(historia)["status"] == "completed"

    asyncio.run(escenario())


# --------------------------------------------------------------- escenario 3


def test_3_una_tool_cumple_lo_que_declara_y_se_explica_al_modelo() -> None:
    """Fase 5. Un esquema declarado y nunca comprobado es documentación que se desincroniza.

    Y lo que llega al modelo es la proyección, no el JSON entero. Verificado contra el
    broker, que además encontró un esquema equivocado que 835 pruebas no vieron.
    """
    from athena.repository_tools import GrepTool
    from athena.schema import violations
    from athena.tools import OutputContract, ToolResult

    tool = GrepTool()
    salida: JSONObject = {
        "query": "add",
        "matches": [{"path": "a.py", "line": 1, "text": "def add"}],
        "truncated": False,
    }

    assert tool.spec.output_contract is OutputContract.ENFORCED
    assert violations(tool.spec.output_schema, salida, where="grep") == ()
    assert tool.project(ToolResult(salida)).model.text == "a.py:1: def add"
    assert ToolContractError.code == "tool_contract_error"


# --------------------------------------------------------------- escenario 4


def test_4_no_haber_podido_comprobar_no_es_haber_fallado() -> None:
    """Fase 13. Contar lo segundo como lo primero le echa la culpa al cambio.

    Verificado contra el broker en los dos caminos, el bucle y el grafo.
    """
    politica = RecoveryPolicy()

    assert not issubclass(VerificationInconclusive, VerificationFailure)
    assert politica.decide(VerificationInconclusive("nada")).action is RecoveryAction.STOP
    assert politica.decide(VerificationFailure("roto")).action is RecoveryAction.RETURN_EVIDENCE


# --------------------------------------------------------------- escenario 5


def test_5_athena_termina_un_encargo_sin_tests_que_pasar(tmp_path: Path) -> None:
    """Fase 8. Un dominio sin comandos ejecutables no puede ser un dominio donde siempre falla.

    Verificado contra el broker sobre una carpeta de actas: glob, read_file, write_file,
    verificado, completado.
    """
    from athena.state import AgentState, AgentStatus, SessionState

    entregable = tmp_path / "informe.md"
    entregable.write_text("# Informe\n\nContenido.\n", encoding="utf-8")

    async def escenario() -> None:
        resultado = await ArtifactVerificationPolicy(("informe.md",)).verify(
            SessionState(
                session_id="s",
                workspace_id="w",
                agent=AgentState(status=AgentStatus.VERIFYING),
                attributes={"files_modified": ["informe.md"]},
            ),
            Workspace.from_path(tmp_path),
            CancellationSource().token,
        )

        assert resultado.permits_completion
        assert "does not establish" in resultado.summary, (
            "un perfil puede declarar evidencia mas debil; lo que no puede es callarselo"
        )
        assert DOCUMENTS.evidence is Evidence.PRODUCED_ARTIFACTS
        assert ProfileRegistry().default is SOFTWARE_ENGINEERING

    asyncio.run(escenario())


# --------------------------------------------------------------- escenario 6


def test_6_revisar_el_encargo_no_hereda_la_evidencia_vieja() -> None:
    """Fase 7. Una verificación que pasó contra el objetivo de ayer no dice nada del de ahora.

    Verificado contra el broker: el modelo obedeció el encargo nuevo y omitió lo cancelado.
    """
    from athena.errors import GoalConflict

    tablero = GoalBoard("Arregla el login")
    tablero.revise("Arregla el registro", base_revision=1, reason="me explique mal")

    with pytest.raises(GoalConflict) as conflicto:
        tablero.revise("Y otra cosa", base_revision=1)

    assert conflicto.value.details["current_revision"] == 2
    recogido = tablero.take()
    assert recogido is not None and recogido.revision == 2
    assert tablero.take() is None, "recogerlo dos veces lo anunciaria dos veces"


# --------------------------------------------------------------- escenario 7


def test_7_un_delegado_contesta_dos_veces_sin_renovar_su_presupuesto() -> None:
    """Fase 9. «Continuable» sin tope compartido es «tantos agentes como quieras, contados
    como uno».

    Verificado contra el broker: el modelo delegó y repreguntó por id.
    """
    from athena.subagents import SubagentBrief, SubagentSession

    sesion = SubagentSession(
        session_id="hijo-1",
        role=SubagentRole.EXPLORER,
        brief=SubagentBrief(objective="Mira a.py"),
        budget=SubagentBudget(max_tool_calls=10, max_follow_ups=1),
    )
    sesion.tool_calls_spent = 4

    assert sesion.remaining().max_tool_calls == 6, "un seguimiento recibio presupuesto entero"
    sesion.follow_ups = 1
    assert sesion.exhausted


# --------------------------------------------------------------- escenario 8


def test_8_athena_aprende_de_la_evidencia_y_lo_viejo_se_nota(tmp_path: Path) -> None:
    """Fase 11. La memoria se leía y no se escribía nunca.

    Verificado contra el broker con dos runs seguidos sobre un repositorio.
    """

    async def escenario() -> None:
        memoria = SqliteProjectMemory(tmp_path / "memory.db")
        orquestador = Orchestrator(
            _Guionizado(),
            InMemoryEventBus(),
            SqliteSessionStore(tmp_path / "s.db"),
            SqliteToolResultStore(tmp_path / "r.db"),
            OrchestrationSettings(memory=memoria),
        )

        aprendidos = await orquestador.learn_from(
            "proyecto",
            VerificationResult(
                VerificationStatus.PASSED,
                (
                    VerificationEvidence(
                        kind="test",
                        summary="tests: passed",
                        metadata={"name": "tests", "command": "pytest -q", "passed": True},
                    ),
                    VerificationEvidence(
                        kind="lint",
                        summary="lint: failed",
                        metadata={"name": "lint", "command": "ruff check .", "passed": False},
                    ),
                ),
                "listo",
            ),
            "run-1",
        )

        assert aprendidos == 1, "un check que fallo no es un comando que funcione"
        (item,) = await memoria.active("proyecto")
        assert item.verification_state.value == "verified"
        assert item.source == "run:run-1"

        viejo = render_for_context([item], now=datetime.now(UTC) + timedelta(days=400))
        assert "stale" in viejo and "pytest -q" in viejo, "lo viejo se etiqueta, no se tira"

    asyncio.run(escenario())


# --------------------------------------------------------------- escenario 9


def test_9_un_chat_escribe_pero_no_ejecuta() -> None:
    """Fase 15. La capacidad más peligrosa por el canal con la identidad más débil, no.

    Verificado arrancándolo contra el broker: rechaza una lista incoherente y, con una
    coherente, levanta el servicio y el poller.
    """
    from athena.adapters.service.runs import CapabilityMode
    from athena_telegram.__main__ import DEFAULT_GRANT_OPTIONS, parse_workspaces
    from athena_telegram.config import TelegramConfigError

    assert DEFAULT_GRANT_OPTIONS.writes is CapabilityMode.ALLOW
    assert DEFAULT_GRANT_OPTIONS.execution is CapabilityMode.OFF
    assert parse_workspaces("1:D:/repo")["telegram:1"].workspace_root == Path("D:/repo")
    with pytest.raises(TelegramConfigError):
        parse_workspaces(None)


# --------------------------------------------------------------- escenario 10


def test_10_deshacer_toca_lo_del_run_y_respeta_lo_ajeno(tmp_path: Path) -> None:
    """Fase 16. El trabajo sin commitear de una persona no es un daño colateral aceptable.

    Verificado contra el broker: un run jerárquico dejó un punto, el rollback restauró
    `calc.py` y dejó intacto un fichero escrito por una persona después.
    """
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    workspace = Workspace.from_path(root)
    libro = RollbackLedger(CheckpointStore(tmp_path / "cp"))
    del_agente = root / "calc.py"
    de_la_persona = root / "mio.md"
    del_agente.write_text("original\n", encoding="utf-8")

    async def escenario() -> None:
        await HookRegistry((checkpointing_hook(libro, workspace),)).run(
            HookContext(HookEvent.PRE_EDIT, "run-1", {"resources": [str(del_agente)]})
        )
        del_agente.write_text("lo cambio el agente\n", encoding="utf-8")
        de_la_persona.write_text("lo escribi yo\n", encoding="utf-8")

        resultado = await libro.roll_back(workspace, scope=RollbackScope.RUN)

        assert del_agente.read_text(encoding="utf-8") == "original\n"
        assert de_la_persona.read_text(encoding="utf-8") == "lo escribi yo\n"
        assert resultado.changed_anything

    asyncio.run(escenario())


# ------------------------------------------------------- el servicio entero


def test_el_servicio_ofrece_todo_lo_que_las_fases_anadieron(tmp_path: Path) -> None:
    """Que cada capa exista no basta: tiene que poder pedirse desde fuera.

    Es la enfermedad recurrente de este proyecto —subsistemas construidos, probados y
    conectados a nada— convertida en una prueba. Cada ruta de esta lista es una fase que
    dejaría de ser alcanzable si alguien la quitase sin darse cuenta.
    """
    import athena.adapters.service.server as modulo

    del tmp_path
    texto = Path(modulo.__file__).read_text(encoding="utf-8")
    for ruta in (
        "/v1/runs/{}/history",  # fase 6
        "/v1/profiles",  # fase 8
        "/v1/runs/{}/goal",  # fase 7
        "/v1/memory",  # fase 11
        "/v1/runs/{}/rollback",  # fase 16
        "/v1/metrics",  # fase 12
    ):
        assert ruta in texto, f"{ruta} dejo de estar enrutada"
