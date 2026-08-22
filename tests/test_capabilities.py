"""Exigir una garantía antes de contar con ella, y nunca a medias.

`ModelCapabilities` existía desde H0 y nadie la consultaba: cada proveedor la declaraba y
ningún llamante la miraba. Una capacidad declarada y jamás exigida no es una garantía, es
un comentario.

Lo que se prueba aquí es la regla y su límite. Donde la garantía es imprescindible, se
rechaza antes de gastar la llamada; donde sólo ayuda, se dice y se sigue. Confundir las dos
cosas tiene un precio en cada dirección: exigir de más prohíbe usos legítimos, exigir de
menos deja pasar la degradación silenciosa.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus, _RunData
from athena.cancellation import CancellationSource, CancellationToken
from athena.capabilities import (
    CapabilityRequirement,
    CapabilityStrength,
    UnsupportedCapabilityError,
    match,
    require,
    requirements_for,
)
from athena.context import ContextBuilder
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from athena.permissions import ReadOnlyPermissionEngine
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.state import SessionState
from athena.stores import InMemoryToolResultStore
from athena.tool_executor import ToolExecutor
from athena.working_state import WorkingState
from athena.workspace import Workspace

_TODO = ModelCapabilities(streaming=True, tool_calls=True, structured_output=True)
_NADA = ModelCapabilities(streaming=False, tool_calls=False, structured_output=False)


class _Counting(ModelProvider):
    """Un proveedor que cuenta cuántas veces le pidieron trabajo de verdad."""

    def __init__(self, capabilities: ModelCapabilities) -> None:
        self._capabilities = capabilities
        self.calls = 0

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        del request
        cancellation.raise_if_cancelled()
        self.calls += 1
        return ModelResponse("hecho", "contador", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


# ------------------------------------------------------------------ el vocabulario


def test_what_is_not_declared_counts_as_absent() -> None:
    """Un proveedor callado no obtiene el beneficio de la duda.

    La duda la pagaría quien contó con la garantía, y la pagaría tarde.
    """
    resultado = match("mudo", _NADA, (CapabilityRequirement("structured_output"),))

    assert not resultado.usable
    assert resultado.missing_required == ("structured_output",)


def test_a_capability_with_a_number_is_compared_as_a_number() -> None:
    """«Admite contexto» no dice si admite el que hace falta.

    Con un booleano habría que descubrir la insuficiencia gastando la llamada, que es
    justo lo que se quiere evitar.
    """
    corto = ModelCapabilities(False, True, True, max_context_tokens=32_000)
    exigencia = (CapabilityRequirement("max_context_tokens", minimum=64_000),)

    assert not match("corto", corto, exigencia).usable

    largo = ModelCapabilities(False, True, True, max_context_tokens=128_000)
    assert match("largo", largo, exigencia).usable


def test_a_missing_preferred_capability_does_not_block() -> None:
    """Preferido y requerido son cosas distintas, y la diferencia es de seguridad.

    Marcar como preferible un requisito real es exactamente cómo se cuela una degradación
    con apariencia de configuración.
    """
    exigencias = (
        CapabilityRequirement("structured_output", CapabilityStrength.PREFERRED),
        CapabilityRequirement("tool_calls"),
    )

    resultado = match("parcial", ModelCapabilities(False, True, False), exigencias)

    assert resultado.usable
    assert resultado.missing_preferred == ("structured_output",)


def test_requiring_says_which_guarantee_was_missing() -> None:
    with pytest.raises(UnsupportedCapabilityError) as fallo:
        require("mudo", _NADA, (CapabilityRequirement("tool_calls"),))

    assert fallo.value.details["missing_required"] == ["tool_calls"]


# ------------------------------------------------------------------ dónde es exigencia


def test_offering_tools_is_not_the_same_as_needing_them() -> None:
    """El bucle manda siempre su catálogo; eso no convierte las herramientas en requisito.

    Un modelo que no sabe llamarlas todavía puede contestar algo que no requiere ninguna.
    Y si hacía falta actuar, lo que ocurre ya tiene mecanismo: sin evidencia, la
    verificación no da el trabajo por bueno.
    """
    exigencias = requirements_for(offers_tools=True)

    assert [r.strength for r in exigencias] == [CapabilityStrength.PREFERRED]
    assert match("sin_tools", _NADA, exigencias).usable


def test_asking_for_a_schema_is_an_actual_requirement() -> None:
    """Quien manda un esquema va a parsear contra él.

    Un proveedor que no lo garantiza devuelve algo ilegible, y el fallo no tiene nada que
    lo explique salvo la ausencia que nadie comprobó.
    """
    exigencias = requirements_for(needs_schema=True)

    assert [r.strength for r in exigencias] == [CapabilityStrength.REQUIRED]
    assert not match("sin_esquema", _NADA, exigencias).usable
    assert match("con_esquema", _TODO, exigencias).usable


# ------------------------------------------------------------------ la invariante crítica


def test_the_provider_is_never_called_when_a_required_guarantee_is_missing(
    tmp_path: Path,
) -> None:
    """Lo que no puede pasar: gastar la llamada y descubrirlo después.

    Comprobar después de llamar sería comprobar tarde. El modelo ya habría trabajado, ya
    se habría pagado, y la respuesta ilegible ya estaría en el transcript.
    """

    async def scenario() -> None:
        proveedor = _Counting(_NADA)
        workspace = Workspace.from_path(tmp_path, "ws")
        registro = ToolRegistry(repository_read_tools())
        bus = InMemoryEventBus()
        vistos: list[RuntimeEvent] = []
        bus.subscribe(vistos.append)
        loop = AgentLoop(
            proveedor,
            registro,
            ToolExecutor(registro, ReadOnlyPermissionEngine(), InMemoryToolResultStore(), bus),
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0),
        )

        with pytest.raises(UnsupportedCapabilityError):
            await loop._require_capabilities(
                ModelRequest(messages=(), response_schema={"type": "object"}),
                _fake_run_data(loop, workspace),
                "peticion-1",
            )

        assert proveedor.calls == 0, "se llamó al modelo pese a faltar la garantía"
        avisos = [event for event in vistos if event.name is EventName.CAPABILITY_MISSING]
        assert avisos and avisos[-1].payload["required"] is True

    asyncio.run(scenario())


def test_a_missing_preference_is_announced_and_nothing_stops(tmp_path: Path) -> None:
    """Se dice y se sigue.

    Saber que se trabajó sin una ventaja explica un resultado peor; impedirlo por eso
    prohibiría trabajar.
    """

    async def scenario() -> None:
        proveedor = _Counting(_NADA)
        workspace = Workspace.from_path(tmp_path, "ws")
        registro = ToolRegistry(repository_read_tools())
        bus = InMemoryEventBus()
        vistos: list[RuntimeEvent] = []
        bus.subscribe(vistos.append)
        loop = AgentLoop(
            proveedor,
            registro,
            ToolExecutor(registro, ReadOnlyPermissionEngine(), InMemoryToolResultStore(), bus),
            ContextBuilder(workspace),
            bus,
            config=AgentLoopConfig(retry_backoff_seconds=0),
        )

        resultado = await loop.run("contesta algo", workspace, CancellationSource().token)

        assert resultado.status is AgentRunStatus.COMPLETED
        avisos = [
            event
            for event in vistos
            if event.name is EventName.CAPABILITY_MISSING and not event.payload["required"]
        ]
        assert avisos, "no se dijo que faltaba una capacidad preferible"
        faltan = avisos[0].payload["missing"]
        assert isinstance(faltan, list)
        assert "tool_calls" in faltan

    asyncio.run(scenario())


def _fake_run_data(loop: AgentLoop, workspace: Workspace) -> _RunData:
    """Lo mínimo que `_require_capabilities` necesita: una sesión con identificador."""
    from athena.agent_loop import _RunData

    return _RunData(
        session=SessionState(session_id="s-1", workspace_id=workspace.workspace_id),
        working=WorkingState(objective="probar"),
    )
