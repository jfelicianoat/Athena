"""Estancamiento: el mismo turno repetido no cuenta como trabajo.

Regresion de un run real (kanban, 22-ago-2026) que gasto tres iteraciones seguidas
listando el mismo directorio y leyendo el mismo README, con resultados identicos, y murio
despues por `budget_exceeded` — un diagnostico que manda a subir el limite cuando el
limite no era el problema.
"""

from __future__ import annotations

import asyncio
from itertools import count
from pathlib import Path

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.errors import ApprovalAbandonedError, NoProgressError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.models import ModelResponse, ModelToolCall
from athena.permissions import PermissionDecision, PermissionRequest, ReadOnlyPermissionEngine
from athena.progress import NoProgressDetector, ProgressVerdict, turn_signature
from athena.recovery import RecoveryAction, RecoveryPolicy
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.workspace import Workspace


def test_a_different_turn_resets_the_streak() -> None:
    detector = NoProgressDetector(warn_after=2, fail_after=3)

    assert detector.observe("a") is ProgressVerdict.PROGRESSING
    assert detector.observe("a") is ProgressVerdict.PROGRESSING
    assert detector.observe("b") is ProgressVerdict.PROGRESSING
    assert detector.repeats == 0


def test_the_streak_warns_before_it_stops() -> None:
    """Primero se le dice, y solo si insiste se corta."""
    detector = NoProgressDetector(warn_after=2, fail_after=3)

    assert detector.observe("a") is ProgressVerdict.PROGRESSING
    assert detector.observe("a") is ProgressVerdict.PROGRESSING
    assert detector.observe("a") is ProgressVerdict.REPEATING
    assert detector.observe("a") is ProgressVerdict.STUCK


def test_the_signature_ignores_the_order_the_model_listed_its_calls() -> None:
    """Pedir A y B no es un turno distinto de pedir B y A."""
    forwards = turn_signature(
        [("list_directory", {"path": "."}), ("read_file", {"path": "README.md"})],
        [{"ok": True, "output": "x"}, {"ok": True, "output": "y"}],
    )
    backwards = turn_signature(
        [("read_file", {"path": "README.md"}), ("list_directory", {"path": "."})],
        [{"ok": True, "output": "y"}, {"ok": True, "output": "x"}],
    )

    assert forwards == backwards


def test_the_signature_counts_the_result_and_not_only_the_request() -> None:
    """Repetir una llamada cuyo resultado cambia es avanzar, no atascarse."""
    calls = [("read_file", {"path": "notes.md"})]

    assert turn_signature(calls, [{"output": "before"}]) != turn_signature(
        calls, [{"output": "after"}]
    )


def test_the_signature_ignores_the_identifiers_that_change_every_turn() -> None:
    """El fallo que un run real encontro y estos tests no.

    El modelo genera un `call_id` nuevo en cada turno y ese id viaja dentro del resultado.
    Con el dentro de la huella, dos turnos identicos nunca daban la misma firma y el
    detector no saltaba jamas: `nemotron-3.5-lightning:30b` lanzo `glob **/test_cola.py`
    seis veces seguidas y agoto el presupuesto sin que nadie lo parase. Los tests no lo
    vieron porque el proveedor de mentira reutilizaba el mismo id en todos los turnos.
    """
    calls = [("glob", {"pattern": "**/test_cola.py"})]

    primero = turn_signature(
        calls, [{"ok": True, "call_id": "c1", "output": "test_cola.py", "reference_uri": "r1"}]
    )
    segundo = turn_signature(
        calls, [{"ok": True, "call_id": "c2", "output": "test_cola.py", "reference_uri": "r2"}]
    )

    assert primero == segundo


def test_a_stuck_run_is_abandoned_instead_of_burning_its_budget(tmp_path: Path) -> None:
    """El bucle, no solo el detector: aqui es donde este proyecto suele quedarse corto."""
    (tmp_path / "README.md").write_text("# Repo\n", encoding="utf-8")

    contador = count(1)

    def _turn() -> ModelResponse:
        # Un `call_id` nuevo cada vez, como hace un modelo de verdad. Reutilizarlo hacia
        # que las huellas coincidiesen por accidente y el test pasaba sin probar nada.
        return ModelResponse(
            "",
            "fake",
            "tool_calls",
            tool_calls=(
                ModelToolCall(f"read-{next(contador)}", "read_file", {"path": "README.md"}),
            ),
        )

    async def scenario() -> None:
        # Ocho turnos identicos disponibles; ninguno deberia llegar a gastarse entero.
        provider = FakeModelProvider([_turn() for _ in range(8)])
        workspace = Workspace.from_path(tmp_path, "test-workspace")
        registry = ToolRegistry(repository_read_tools())
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        executor = ToolExecutor(
            registry, ReadOnlyPermissionEngine(), InMemoryToolResultStore(), bus
        )
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0, max_iterations=12),
        )

        result = await loop.run("Read the readme", workspace, CancellationSource().token)

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        # El codigo importa tanto como el fallo: `budget_exceeded` mandaria a subir el
        # limite, y subir el limite solo compra mas vueltas iguales.
        assert result.error.code == "no_progress"
        # Se corta bastante antes del presupuesto, que era el punto.
        assert len(provider.requests) < 8

        warned = [
            event
            for event in events
            if event.name is EventName.RECOVERY_ACTION
            and event.payload.get("action") == "no_progress"
        ]
        # Avisado primero, abandonado despues.
        assert [event.payload["verdict"] for event in warned] == ["repeating", "stuck"]

    asyncio.run(scenario())


def test_a_stuck_run_is_told_before_it_is_stopped(tmp_path: Path) -> None:
    """El aviso tiene que llegarle al modelo, no solo al log de eventos."""
    (tmp_path / "README.md").write_text("# Repo\n", encoding="utf-8")

    contador = count(1)

    def _turn() -> ModelResponse:
        # Un `call_id` nuevo cada vez, como hace un modelo de verdad. Reutilizarlo hacia
        # que las huellas coincidiesen por accidente y el test pasaba sin probar nada.
        return ModelResponse(
            "",
            "fake",
            "tool_calls",
            tool_calls=(
                ModelToolCall(f"read-{next(contador)}", "read_file", {"path": "README.md"}),
            ),
        )

    async def scenario() -> None:
        provider = FakeModelProvider([_turn() for _ in range(8)])
        workspace = Workspace.from_path(tmp_path, "test-workspace")
        registry = ToolRegistry(repository_read_tools())
        bus = InMemoryEventBus()
        executor = ToolExecutor(
            registry, ReadOnlyPermissionEngine(), InMemoryToolResultStore(), bus
        )
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0, max_iterations=12),
        )

        result = await loop.run("Read the readme", workspace, CancellationSource().token)

        assert isinstance(result.error, NoProgressError)
        # La ultima peticion al modelo es la que siguio al aviso: si el texto no viaja
        # ahi, el modelo nunca se entera de que esta repitiendose.
        assert any(
            "same tool calls as the previous turn" in (message.content or "")
            for message in provider.requests[-1].messages
        )

    asyncio.run(scenario())


def test_no_progress_is_not_retried() -> None:
    """Reintentar es justo lo que ya se ha demostrado que no funciona."""
    assert RecoveryPolicy().decide(NoProgressError("stuck")).action is RecoveryAction.ABORT


class _AbandoningPrompt:
    """Nadie al otro lado: cada pregunta acaba en abandono."""

    async def confirm(self, request: PermissionRequest) -> PermissionDecision:
        raise ApprovalAbandonedError(
            "3 approval requests went unanswered; abandoning the run",
            details={"run_id": "run-1"},
        )


class _AskingPermissionEngine:
    def decide(self, request: PermissionRequest) -> PermissionDecision:
        del request
        return PermissionDecision.ASK


def test_an_abandoned_approval_actually_abandons_the_run(tmp_path: Path) -> None:
    """El mensaje del error promete abandonar el run. Antes no lo hacia.

    Regresion del run real: `ApprovalAbandonedError` se lanzaba tres veces —«3», «4», «5»
    peticiones sin contestar— y el bucle la trataba como un fallo de tool cualquiera,
    devolvia `ok: false` al modelo y seguia. El run murio media hora despues por
    presupuesto, habiendo escrito entre medias ficheros que nadie autorizo mirar.

    `RecoveryDirective.ends_run` ya decia que ABORT termina el run; lo que faltaba era que
    alguien lo mirase.
    """
    (tmp_path / "README.md").write_text("# Repo\n", encoding="utf-8")

    async def scenario() -> None:
        provider = FakeModelProvider(
            [
                ModelResponse(
                    "",
                    "fake",
                    "tool_calls",
                    tool_calls=(ModelToolCall("read-1", "read_file", {"path": "README.md"}),),
                ),
                ModelResponse("Should never be reached.", "fake", "stop"),
            ]
        )
        workspace = Workspace.from_path(tmp_path, "test-workspace")
        registry = ToolRegistry(repository_read_tools())
        bus = InMemoryEventBus()
        executor = ToolExecutor(
            registry,
            _AskingPermissionEngine(),
            InMemoryToolResultStore(),
            bus,
            prompt=_AbandoningPrompt(),
        )
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0, max_iterations=12),
        )

        result = await loop.run("Read the readme", workspace, CancellationSource().token)

        assert result.status is AgentRunStatus.FAILED
        assert result.error is not None
        assert result.error.code == "approval_abandoned"
        # Una sola vuelta: el run no siguio preguntandole a nadie.
        assert len(provider.requests) == 1

    asyncio.run(scenario())
