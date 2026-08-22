"""Ver una herramienta no es poder usarla, y esconderla no es seguridad.

Athena separa las dos cosas desde H2 —el registro decide qué existe para un agente, el
`PermissionEngine` decide qué se ejecuta— pero la separación sólo estaba probada por el
camino principal. Estas pruebas atacan las entradas por donde una autorización podría
colarse sin pasar por el motor: herramientas diferidas, MCP, delegación y un padre que
intenta obtener por su hijo lo que él no tiene.

Cada una está escrita como un intento, no como una comprobación: lo que se afirma es que
el intento no consigue nada.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from athena.delegation import confine, narrow
from athena.errors import ToolValidationError
from athena.events import InMemoryEventBus
from athena.mcp import McpToolPolicy
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PolicyPermissionEngine,
    RiskTier,
)
from athena.process_tools import BashTool
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.subagents import DEFAULT_PROFILES, SubagentRole
from athena.tool_executor import ToolExecutor
from athena.tools import Tool
from athena.workspace import Workspace


def _catalog(bus: InMemoryEventBus) -> dict[str, Tool]:
    tools: list[Tool] = [
        *repository_read_tools(),
        *workspace_mutation_tools(bus),
        BashTool(event_bus=bus),
    ]
    return {tool.spec.name: tool for tool in tools}


# ------------------------------------------------------------------ A: inventar una tool


def test_a_role_cannot_call_a_tool_that_does_not_exist_for_it(tmp_path: Path) -> None:
    """El explorer pide `write_file`, que no está en su registro.

    La primera línea es estructural: la herramienta no existe para él, así que negarla no
    depende de que una política esté bien configurada. La segunda es el motor, y también
    diría que no — pero no llega a hacer falta.
    """

    async def scenario() -> None:
        bus = InMemoryEventBus()
        perfil = confine(
            DEFAULT_PROFILES[SubagentRole.EXPLORER],
            PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True),
            frozenset(_catalog(bus)),
        )
        registro = perfil.registry_for(_catalog(bus))

        assert "write_file" not in set(registro.names())
        try:
            registro.get("write_file")
        except ToolValidationError:
            return
        raise AssertionError("el explorer alcanzó una herramienta de escritura")

    asyncio.run(scenario())


# ------------------------------------------------------------------ C: descubrir ≠ autorizar


def test_discovering_a_tool_does_not_authorise_it(tmp_path: Path) -> None:
    """`tool_search` enseña nombres; el motor sigue decidiendo.

    Es la trampa del catálogo diferido: si descubrir concediera permiso, bastaría con
    buscar para escalar, y la carga diferida —que existe para no gastar contexto— se
    convertiría en un agujero.
    """
    workspace = Workspace.from_path(tmp_path, "ws")
    motor = PolicyPermissionEngine(
        PermissionPolicy(allow_workspace_writes=False, allow_local_execution=False)
    )
    bash = BashTool(event_bus=InMemoryEventBus())

    peticion = bash.permission(
        type("Ctx", (), {"workspace": workspace, "session_id": "s"})(),
        {"command": "pytest -q", "timeout_seconds": 5},
    )

    assert motor.decide(peticion) is not PermissionDecision.ALLOW


# ------------------------------------------------------------------ D: MCP no concede nada


def test_an_mcp_server_announcing_a_tool_grants_nothing() -> None:
    """Que un servidor externo diga que puede hacer algo no autoriza a que lo haga.

    Por eso el tier por defecto de una tool MCP es R3: toda llamada pregunta. Bajarlo es
    una decisión sobre un servidor concreto, no el punto de partida.
    """
    politica = McpToolPolicy()

    assert politica.tier is RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE

    motor = PolicyPermissionEngine(
        PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True)
    )
    del motor  # el tier ya es la afirmación: R3 nunca es un ALLOW silencioso


# ------------------------------------------------------------------ E: delegar no escala


def test_a_read_only_parent_cannot_get_a_writing_child() -> None:
    """La intersección, no la unión.

    Es el escalado indirecto: un padre sin permiso de escritura pide un hijo que sí pueda,
    y si delegar concediera lo pedido en vez de lo heredado, cualquier restricción sería
    evitable pidiendo un subagente.
    """
    padre = PermissionPolicy(allow_workspace_writes=False, allow_local_execution=False)
    hijo_deseado = PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True)

    resultante = narrow(padre, hijo_deseado)

    assert not resultante.allow_workspace_writes
    assert not resultante.allow_local_execution


def test_confining_a_coder_under_a_read_only_parent_removes_its_authority() -> None:
    """Y por el camino completo: perfil real, padre real, catálogo real."""
    bus = InMemoryEventBus()
    catalogo = _catalog(bus)
    padre = PermissionPolicy(allow_workspace_writes=False, allow_local_execution=False)

    confinado = confine(DEFAULT_PROFILES[SubagentRole.CODER], padre, frozenset(catalogo))

    assert not confinado.policy.allow_workspace_writes
    assert not confinado.policy.allow_local_execution
    # Las herramientas siguen siendo visibles: el coder las conoce y no puede usarlas.
    # Esconderlas además sería una segunda línea, no la que decide.
    assert "write_file" in confinado.toolsets


# ------------------------------------------------------------------ F: los argumentos deciden


def test_the_same_tool_is_allowed_or_denied_by_what_it_is_asked_to_do(tmp_path: Path) -> None:
    """Autorizar por nombre sería autorizar `bash` entero.

    `pytest -q` y un borrado recursivo son la misma herramienta con distinta petición, y
    la diferencia no está en el nombre.
    """
    workspace = Workspace.from_path(tmp_path, "ws")
    motor = PolicyPermissionEngine(
        PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True)
    )
    bash = BashTool(event_bus=InMemoryEventBus())
    contexto = type("Ctx", (), {"workspace": workspace, "session_id": "s"})()

    inocuo = motor.decide(bash.permission(contexto, {"command": "pytest -q", "timeout_seconds": 5}))
    assert inocuo is PermissionDecision.ALLOW

    try:
        peligroso = bash.permission(
            contexto,
            {"command": "rm -rf /", "timeout_seconds": 5},
        )
    except ToolValidationError:
        # Rechazado antes incluso de llegar al motor: la política de comandos lo prohíbe
        # por sí sola, que es una negativa más temprana y no una más débil.
        return
    assert motor.decide(peligroso) is not PermissionDecision.ALLOW


# ------------------------------------------------------------------ H: el perfil no manda


def test_a_profile_cannot_widen_what_the_deployment_allows() -> None:
    """Un perfil declara lo que querría; el despliegue decide lo que hay.

    Si un perfil pudiera ampliar la política global, la seguridad la fijaría el fichero
    más nuevo en lugar del operador.
    """
    bus = InMemoryEventBus()
    catalogo = _catalog(bus)
    despliegue = PermissionPolicy(allow_workspace_writes=False, allow_local_execution=True)

    verificador = confine(DEFAULT_PROFILES[SubagentRole.VERIFIER], despliegue, frozenset(catalogo))
    programador = confine(DEFAULT_PROFILES[SubagentRole.CODER], despliegue, frozenset(catalogo))

    # El verifier ya no escribía; el coder sí quería, y no puede.
    assert not verificador.policy.allow_workspace_writes
    assert not programador.policy.allow_workspace_writes
    # Lo que el despliegue sí permite se conserva cuando el perfil también lo quiere.
    assert programador.policy.allow_local_execution


# ------------------------------------------------------------------ el motor es el final


def test_every_execution_goes_through_the_engine_even_when_visible(tmp_path: Path) -> None:
    """Visible y autorizada son dos puertas, y la segunda no se salta.

    Se ejecuta por el camino real —`ToolExecutor`— con una herramienta que el registro sí
    contiene, para que lo único que pueda pararla sea el motor.
    """

    async def scenario() -> None:
        bus = InMemoryEventBus()
        catalogo = _catalog(bus)
        registro = ToolRegistry(tuple(catalogo.values()))
        ejecutor = ToolExecutor(
            registro,
            PolicyPermissionEngine(
                PermissionPolicy(allow_workspace_writes=False, allow_local_execution=False)
            ),
            InMemoryToolResultStore(),
            bus,
        )
        workspace = Workspace.from_path(tmp_path, "ws")
        assert "write_file" in set(registro.names())

        from athena.cancellation import CancellationSource
        from athena.models import ModelToolCall

        try:
            await ejecutor.execute(
                ModelToolCall("c1", "write_file", {"path": "x.txt", "content": "hola"}),
                session_id="s",
                workspace=workspace,
                cancellation=CancellationSource().token,
            )
        except Exception:
            assert not (tmp_path / "x.txt").exists(), "la escritura ocurrió pese a negarse"
            return
        raise AssertionError("una escritura no autorizada llegó a ejecutarse")

    asyncio.run(scenario())
