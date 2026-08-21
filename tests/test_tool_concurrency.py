"""Qué llamadas de un turno se solapan, observado y no supuesto.

`ConcurrencyScheduler` sabía calcular olas seguras desde que se escribió, y hasta ahora
nadie se lo preguntaba: el bucle ejecutaba una llamada detrás de otra. Estas pruebas miran
el solapamiento en sí, con una barrera, en vez de confiar en tiempos: si dos llamadas
corren a la vez, ambas llegan a la barrera y ésta se abre; si van en serie, la primera
espera sola hasta agotar su plazo.

Medir con relojes diría lo mismo la mayoría de las veces y mentiría bajo carga, que es
justo cuando se ejecutan las suites.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.events import InMemoryEventBus
from athena.models import ModelResponse, ModelToolCall
from athena.permissions import (
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
    RiskTier,
)
from athena.registry import ToolRegistry
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.tools import ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject
from athena.workspace import Workspace


class _AllowAll:
    """Autoriza todo: lo que se mide aquí es el solapamiento, no la política.

    Implementa `decide`, que es el único método del contrato. Heredar del Protocol y
    escribir otro nombre deja un motor que devuelve `None` y un fallo a cuatro capas de
    distancia, en el ejecutor.
    """

    def decide(self, request: PermissionRequest) -> PermissionDecision:
        del request
        return PermissionDecision.ALLOW


class _Barrier:
    """Cuenta cuántas llamadas coinciden dentro de la herramienta a la vez."""

    def __init__(self, expected: int, *, timeout: float = 1.0) -> None:
        self.expected = expected
        self.timeout = timeout
        self.arrived = 0
        self.peak = 0
        self._open = asyncio.Event()

    async def wait(self) -> bool:
        """True si llegó compañía antes del plazo."""
        self.arrived += 1
        self.peak = max(self.peak, self.arrived)
        if self.arrived >= self.expected:
            self._open.set()
        try:
            await asyncio.wait_for(self._open.wait(), timeout=self.timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self.arrived -= 1


class _Meeting:
    """Una herramienta que no hace nada salvo dejarse observar."""

    def __init__(self, name: str, barrier: _Barrier, *, concurrency_safe: bool) -> None:
        self._name = name
        self._barrier = barrier
        self._safe = concurrency_safe

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            description="Espera a ver si alguien más está dentro.",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            output_schema={"type": "object"},
            risk=RiskLevel.LOW,
            max_result_size_chars=1_000,
        )

    def validate(self, arguments: JSONObject) -> JSONObject:
        return dict(arguments)

    def is_read_only(self, arguments: JSONObject) -> bool:
        del arguments
        return self._safe

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        del arguments
        return self._safe

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        del context
        return PermissionRequest(
            tool_name=self._name,
            operation="meet",
            workspace=Workspace.from_path(Path.cwd()),
            risk=RiskLevel.LOW,
            tier=RiskTier.R0_READ_ONLY,
            is_read_only=self._safe,
            is_destructive=False,
            is_concurrency_safe=self._safe,
            arguments=arguments,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        del context, cancellation
        together = await self._barrier.wait()
        return ToolResult({"together": together, "path": arguments.get("path", "")})


def _turn(*calls: tuple[str, str, str]) -> ModelResponse:
    return ModelResponse(
        "",
        "fake",
        "tool_calls",
        tool_calls=tuple(
            ModelToolCall(call_id, name, {"path": path}) for call_id, name, path in calls
        ),
    )


def _run(root: Path, tools: tuple[object, ...], turn: ModelResponse) -> AgentRunStatus:
    async def scenario() -> AgentRunStatus:
        workspace = Workspace.from_path(root, "test-workspace")
        registry = ToolRegistry(tools)  # type: ignore[arg-type]
        bus = InMemoryEventBus()
        executor = ToolExecutor(registry, _AllowAll(), InMemoryToolResultStore(), bus)
        loop = AgentLoop(
            FakeModelProvider([turn, ModelResponse("Listo.", "fake", "stop")]),
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0),
        )
        result = await loop.run("mirar dos cosas", workspace, CancellationSource().token)
        return result.status

    return asyncio.run(scenario())


def test_two_safe_reads_in_one_turn_actually_overlap(tmp_path: Path) -> None:
    """Lo que el planificador prometía y el bucle no le pedía.

    Las dos llamadas se declaran seguras y tocan ficheros distintos, así que caben en la
    misma ola. La barrera sólo se abre si coinciden dentro.
    """
    barrier = _Barrier(expected=2)
    tools = (
        _Meeting("look_here", barrier, concurrency_safe=True),
        _Meeting("look_there", barrier, concurrency_safe=True),
    )

    status = _run(
        tmp_path,
        tools,
        _turn(("a", "look_here", "uno.txt"), ("b", "look_there", "dos.txt")),
    )

    assert status is AgentRunStatus.COMPLETED
    assert barrier.peak == 2, "las lecturas seguras siguieron yendo de una en una"


def test_a_tool_that_did_not_claim_to_be_safe_runs_alone(tmp_path: Path) -> None:
    """El valor por defecto es no solaparse, y no cambia porque convenga.

    Una herramienta que no se declara segura se ejecuta sola aunque su compañera sí lo
    haga: la seguridad de una ola la deciden las dos, no la más optimista.
    """
    barrier = _Barrier(expected=2, timeout=0.4)
    tools = (
        _Meeting("look_here", barrier, concurrency_safe=True),
        _Meeting("touch_this", barrier, concurrency_safe=False),
    )

    status = _run(
        tmp_path,
        tools,
        _turn(("a", "look_here", "uno.txt"), ("b", "touch_this", "dos.txt")),
    )

    assert status is AgentRunStatus.COMPLETED
    assert barrier.peak == 1, "una herramienta no declarada segura se solapó igualmente"


def test_the_transcript_keeps_the_order_the_model_asked_for(tmp_path: Path) -> None:
    """Solaparse cambia cuándo termina cada llamada, no cómo se cuenta.

    Si el orden del transcript siguiera al de finalización, el modelo leería sus propias
    llamadas reordenadas de un turno a otro sin que nada lo explique.
    """

    async def scenario() -> None:
        barrier = _Barrier(expected=2)
        tools = (
            _Meeting("look_here", barrier, concurrency_safe=True),
            _Meeting("look_there", barrier, concurrency_safe=True),
        )
        workspace = Workspace.from_path(tmp_path, "test-workspace")
        registry = ToolRegistry(tools)  # type: ignore[arg-type]
        bus = InMemoryEventBus()
        executor = ToolExecutor(registry, _AllowAll(), InMemoryToolResultStore(), bus)
        provider = FakeModelProvider(
            [
                _turn(("primera", "look_here", "uno.txt"), ("segunda", "look_there", "dos.txt")),
                ModelResponse("Listo.", "fake", "stop"),
            ]
        )
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0),
        )

        await loop.run("mirar dos cosas", workspace, CancellationSource().token)

        entregados = [
            message.tool_call_id
            for message in provider.requests[-1].messages
            if message.tool_call_id
        ]
        assert entregados == ["primera", "segunda"]

    asyncio.run(scenario())


def test_a_duplicate_call_id_is_refused_without_disturbing_the_others(tmp_path: Path) -> None:
    """Los ids vienen del modelo y pueden repetirse.

    El rechazo del duplicado tiene que ocupar su sitio y no el de la llamada a la que
    duplica: guardar los resultados por identificador dejaría que uno pisara al otro.
    """

    async def scenario() -> None:
        barrier = _Barrier(expected=1, timeout=0.2)
        tools = (_Meeting("look_here", barrier, concurrency_safe=True),)
        workspace = Workspace.from_path(tmp_path, "test-workspace")
        registry = ToolRegistry(tools)  # type: ignore[arg-type]
        bus = InMemoryEventBus()
        executor = ToolExecutor(registry, _AllowAll(), InMemoryToolResultStore(), bus)
        provider = FakeModelProvider(
            [
                _turn(("misma", "look_here", "uno.txt"), ("misma", "look_here", "dos.txt")),
                ModelResponse("Listo.", "fake", "stop"),
            ]
        )
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0),
        )

        await loop.run("mirar dos veces lo mismo", workspace, CancellationSource().token)

        mensajes = [
            message.content
            for message in provider.requests[-1].messages
            if message.tool_call_id == "misma"
        ]
        assert len(mensajes) == 2
        assert "uno.txt" in mensajes[0], "el resultado real perdió su sitio"
        assert "duplicated" in mensajes[1], "el duplicado no fue rechazado"

    asyncio.run(scenario())
