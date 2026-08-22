"""Quién ejecuta un subagente deja de estar decidido en el sitio que lo pide.

Lo que se prueba aquí no es que el proveedor nativo funcione —eso ya lo prueban las suites
de subagentes y del ejecutor— sino que el ejecutor deja de saber quién es. Por eso hay un
segundo proveedor falso: si el grafo funciona con él, no puede estar dependiendo del
primero.

Y la otra mitad: un proveedor que no ofrece lo que hace falta se rechaza en vez de usarse a
medias. La degradación silenciosa es el fallo que este contrato existe para impedir.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.agent_loop import AgentRunStatus
from athena.cancellation import CancellationSource
from athena.errors import AthenaRuntimeError
from athena.events import InMemoryEventBus
from athena.stores import InMemoryToolResultStore
from athena.subagent_provider import (
    NativeAthenaSubagentProvider,
    NoSuitableProviderError,
    SubagentCapabilities,
    SubagentProviderRegistry,
    SubagentService,
    SubagentStartRequest,
)
from athena.subagents import SubagentBrief, SubagentResult, SubagentRole, SubagentRunner
from athena.testing import FakeModelProvider
from athena.workspace import Workspace


class _Recording:
    """Un proveedor que no es Athena y deja constancia de que lo llamaron."""

    def __init__(self, name: str, capabilities: SubagentCapabilities) -> None:
        self._name = name
        self._capabilities = capabilities
        self.started: list[SubagentStartRequest] = []

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> SubagentCapabilities:
        return self._capabilities

    async def start(self, request: SubagentStartRequest) -> SubagentResult:
        self.started.append(request)
        return SubagentResult(
            role=request.role,
            status=AgentRunStatus.COMPLETED,
            session_id=f"{self._name}-1",
            answer="hecho por otro",
        )


_TODO = SubagentCapabilities(
    structured_output=True,
    tool_filtering=True,
    isolated_workspace=True,
    continuation=True,
    cancellation=True,
    streaming=True,
    depth_limit=4,
)


def _brief() -> SubagentBrief:
    return SubagentBrief(objective="mirar una cosa")


# ------------------------------------------------------------------ el contrato


def test_a_provider_that_declares_nothing_qualifies_for_nothing() -> None:
    """Lo no declarado se lee como ausente, no como probable.

    Es la lectura que falla cerrada: un proveedor callado no obtiene el beneficio de la
    duda, porque la duda la pagaría quien confió en una garantía que nadie dio.
    """
    silencioso = SubagentCapabilities()

    faltan = silencioso.satisfies(SubagentCapabilities(tool_filtering=True))

    assert faltan == ("tool_filtering",)


def test_the_gaps_are_named_and_not_just_counted() -> None:
    """Un «no» sin «qué» deja a quien pregunta sin nada que contar."""
    pobre = SubagentCapabilities(cancellation=True)

    faltan = pobre.satisfies(_TODO)

    assert set(faltan) >= {"structured_output", "tool_filtering", "continuation", "streaming"}
    assert "cancellation" not in faltan


def test_a_depth_limit_is_a_number_and_not_a_flag() -> None:
    """Pedir cinco niveles a quien admite tres es incompatible aunque ambos «admitan»."""
    tres = SubagentCapabilities(depth_limit=3)

    assert tres.satisfies(SubagentCapabilities(depth_limit=5)) == ("depth_limit",)
    assert tres.satisfies(SubagentCapabilities(depth_limit=2)) == ()


# ------------------------------------------------------------------ la selección


def test_the_registry_refuses_rather_than_settling(tmp_path: Path) -> None:
    """Ninguno vale, así que no se usa el que menos mal está.

    Elegir «el más parecido» exigiría una noción de distancia que nadie ha definido, y
    produciría ejecuciones que cumplen casi todo — que es la forma de fallar que este
    contrato existe para impedir.
    """
    del tmp_path
    registro = SubagentProviderRegistry(
        (
            _Recording("pobre", SubagentCapabilities(cancellation=True)),
            _Recording("menos_pobre", SubagentCapabilities(cancellation=True, tool_filtering=True)),
        )
    )

    with pytest.raises(NoSuitableProviderError) as fallo:
        registro.select(_TODO)

    # Y dice de cada uno qué le faltaba, que es lo que permite arreglarlo.
    rechazados = fallo.value.details["rejected"]
    assert isinstance(rechazados, dict)
    assert set(rechazados) == {"pobre", "menos_pobre"}
    faltantes = rechazados["pobre"]
    assert isinstance(faltantes, list)
    assert "structured_output" in faltantes


def test_registration_order_is_the_preference_order() -> None:
    """El primero que cumple, no el más capaz.

    «El más capaz» daría trabajo pesado a quien no lo necesitaba; y sin criterio explícito,
    la elección la acabaría fijando el orden de un diccionario.
    """
    basico = SubagentCapabilities(tool_filtering=True, cancellation=True)
    primero = _Recording("primero", basico)
    segundo = _Recording("segundo", _TODO)
    registro = SubagentProviderRegistry((primero, segundo))

    assert registro.select(basico) is primero


def test_an_empty_registry_says_so_instead_of_returning_nothing() -> None:
    with pytest.raises(NoSuitableProviderError, match="No subagent provider"):
        SubagentProviderRegistry().select(SubagentCapabilities())


def test_two_providers_cannot_share_a_name() -> None:
    """Un nombre repetido haría que la selección dependiera de quién se registró antes."""
    registro = SubagentProviderRegistry((_Recording("uno", SubagentCapabilities()),))

    with pytest.raises(AthenaRuntimeError, match="already registered"):
        registro.register(_Recording("uno", SubagentCapabilities()))


# ------------------------------------------------------------------ el servicio


def test_a_role_carries_its_own_requirements(tmp_path: Path) -> None:
    """Un explorer necesita contestar en una forma legible sin modelo de por medio.

    No es una comodidad: su trabajo entero es pasar hallazgos hacia arriba, y un proveedor
    que devuelva prosa obligaría al padre a interpretarla, que es exactamente lo que la
    delegación evita.
    """

    del tmp_path

    async def scenario() -> None:
        sin_estructura = _Recording(
            "prosa", SubagentCapabilities(tool_filtering=True, cancellation=True)
        )
        servicio = SubagentService(SubagentProviderRegistry((sin_estructura,)))

        with pytest.raises(NoSuitableProviderError):
            await servicio.delegate(
                SubagentRole.EXPLORER,
                _brief(),
                Workspace.from_path(Path.cwd()),
                CancellationSource().token,
            )
        assert sin_estructura.started == [], "se llamó a un proveedor que no cumplía"

    asyncio.run(scenario())


def test_the_provider_method_is_never_called_when_the_check_fails(tmp_path: Path) -> None:
    """La invariante crítica: si falta una garantía requerida, no se ejecuta nada.

    Comprobar después de arrancar sería comprobar tarde: el trabajo ya habría empezado sin
    la garantía, y pararlo entonces no deshace lo que hiciera.
    """

    del tmp_path

    async def scenario() -> None:
        incapaz = _Recording("incapaz", SubagentCapabilities())
        servicio = SubagentService(SubagentProviderRegistry((incapaz,)))

        with pytest.raises(NoSuitableProviderError):
            await servicio.delegate(
                SubagentRole.CODER,
                _brief(),
                Workspace.from_path(Path.cwd()),
                CancellationSource().token,
            )

        assert incapaz.started == []

    asyncio.run(scenario())


def test_the_service_delegates_to_whoever_qualifies(tmp_path: Path) -> None:
    """Y cuando alguien cumple, se le llama con la petición entera."""

    del tmp_path

    async def scenario() -> None:
        apto = _Recording("apto", _TODO)
        servicio = SubagentService(SubagentProviderRegistry((apto,)))

        resultado = await servicio.delegate(
            SubagentRole.CODER,
            _brief(),
            Workspace.from_path(Path.cwd()),
            CancellationSource().token,
            parent_session_id="padre-1",
        )

        assert resultado.answer == "hecho por otro"
        assert apto.started[0].parent_session_id == "padre-1"
        assert apto.started[0].brief.objective == "mirar una cosa"

    asyncio.run(scenario())


# ------------------------------------------------------------------ el nativo


def test_the_native_provider_declares_what_athena_actually_does(tmp_path: Path) -> None:
    """Lo que hoy es cierto, y nada más.

    `continuation` era falso porque no existía. Ahora existe —se le puede volver a
    preguntar a un delegado dentro de su tope, ADR-030— y por eso pasa a ser cierto:
    la lista describe lo que hay, no lo que gustaría.

    `streaming` e `isolated_workspace` siguen siendo falsos por el motivo original.
    Declararlos convertiría esta descripción en un deseo, y quien la consultara para
    decidir estaría decidiendo con un dato inventado.
    """
    del tmp_path
    runner = SubagentRunner(
        FakeModelProvider([]), {}, InMemoryEventBus(), InMemoryToolResultStore()
    )

    capacidades = NativeAthenaSubagentProvider(runner).capabilities()

    assert capacidades.structured_output
    assert capacidades.tool_filtering
    assert capacidades.cancellation
    assert capacidades.continuation
    assert not capacidades.streaming
    assert not capacidades.isolated_workspace


def test_the_native_provider_satisfies_every_role_athena_has(tmp_path: Path) -> None:
    """Si no cumpliera, el runtime actual no podría delegar en sí mismo."""
    del tmp_path
    runner = SubagentRunner(
        FakeModelProvider([]), {}, InMemoryEventBus(), InMemoryToolResultStore()
    )
    nativo = NativeAthenaSubagentProvider(runner)
    registro = SubagentProviderRegistry((nativo,))

    for role in SubagentRole:
        from athena.subagent_provider import _required_for

        assert registro.select(_required_for(role)) is nativo


# ------------------------------------------------------------------ el ejecutor no sabe quién


def test_the_graph_runs_on_a_provider_that_is_not_athena(tmp_path: Path) -> None:
    """La prueba de que la costura existe.

    Si el grafo termina un plan con un proveedor que no construye ningún `AgentLoop`, es
    que ya no depende de que lo haya. Lo que se afirma no es que este proveedor sirva para
    nada real —no hace nada— sino que el coordinador dejó de elegir implementación.
    """

    async def scenario() -> None:
        from athena.cancellation import CancellationSource
        from athena.graph_executor import GraphExecutor
        from athena.planning import TaskGraph, TaskNode
        from athena.state import ExecutionOutcome
        from athena.tasks import TaskManager

        ajeno = _Recording("ajeno", _TODO)
        servicio = SubagentService(SubagentProviderRegistry((ajeno,)))
        manager = TaskManager()
        ejecutor = GraphExecutor(servicio, manager, InMemoryEventBus())
        grafo = TaskGraph.build(
            [
                TaskNode(
                    id="T01",
                    goal="mirar algo",
                    expected_output="algo mirado",
                    acceptance_criteria=("se puede comprobar",),
                    suggested_role=SubagentRole.CODER,
                )
            ]
        )

        resultado = await ejecutor.execute(
            grafo, Workspace.from_path(tmp_path), CancellationSource().token, run_id="r"
        )
        await manager.shutdown()

        assert resultado.outcome is ExecutionOutcome.COMPLETED
        assert len(ajeno.started) == 1, "el grafo no pasó por el proveedor registrado"
        assert ajeno.started[0].role is SubagentRole.CODER

    asyncio.run(scenario())
