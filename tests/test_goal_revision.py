"""Cambiar el encargo con el trabajo empezado.

Tres problemas distintos y aquí se prueban por separado: quién gana si dos personas
revisan a la vez, cuándo se aplica el cambio, y qué pasa con lo que ya estaba hecho. El
tercero es el que se puede resolver mal sin que nadie lo note: **la evidencia obtenida bajo
una revisión no demuestra la siguiente**, y heredarla sería la forma más barata de dar por
bueno un trabajo que nadie pidió.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.errors import GoalConflict, ToolValidationError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.goals import Goal, GoalBoard, announcement, summarise
from athena.models import ModelResponse, ModelToolCall
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.verification import LoopCompletionVerificationPolicy
from athena.workspace import Workspace

# -- concurrencia optimista --------------------------------------------------


def test_quien_escribe_sobre_una_version_vieja_no_pisa_a_nadie() -> None:
    """Dos personas mirando el mismo run no se sobrescriben sin enterarse.

    No se fusiona ni se pisa: fusionar dos encargos escritos en prosa no lo sabe hacer
    nadie, y pisar convierte el trabajo de otro en un cambio que nunca vio.
    """
    tablero = GoalBoard("Arregla el login")
    tablero.revise("Arregla el login y el registro", base_revision=1, reason="falta el alta")

    with pytest.raises(GoalConflict) as conflicto:
        tablero.revise("Arregla solo el registro", base_revision=1)

    assert conflicto.value.details["current_revision"] == 2
    assert conflicto.value.details["current"] == "Arregla el login y el registro", (
        "quien llegó tarde tiene que recibir el objetivo actual, no sólo un error"
    )
    assert tablero.current.text == "Arregla el login y el registro"


def test_revisar_con_el_mismo_texto_no_es_una_revision() -> None:
    """Crearla haría que el bucle parase para que le contasen lo que ya sabía.

    Y dejaría en el registro un cambio que no lo fue, que es peor: un run con cinco
    revisiones idénticas parece un encargo que nadie tenía claro.
    """
    tablero = GoalBoard("Arregla el login")

    devuelto = tablero.revise("  Arregla el login  ", base_revision=1)

    assert devuelto.revision == 1
    assert len(tablero.history()) == 1
    assert tablero.pending is None


def test_un_objetivo_vacio_no_es_un_objetivo() -> None:
    tablero = GoalBoard("Arregla el login")

    with pytest.raises(ToolValidationError):
        tablero.revise("   ", base_revision=1)


def test_la_historia_se_guarda_entera_y_no_solo_el_final() -> None:
    """Un run que acabó haciendo otra cosa es explicable si consta cuándo cambió."""
    tablero = GoalBoard("A")
    tablero.revise("B", base_revision=1, reason="me equivoqué")
    tablero.revise("C", base_revision=2, reason="y otra vez")

    assert [goal.text for goal in tablero.history()] == ["A", "B", "C"]
    assert summarise(tablero.history()) == {
        "revision": 3,
        "revised": True,
        "reasons": ["me equivoqué", "y otra vez"],
    }


# -- entrega -----------------------------------------------------------------


def test_escrito_no_es_recogido() -> None:
    """Que un cambio esté escrito no quiere decir que haya llegado a tiempo de nada.

    El cliente necesita poder distinguirlo: prometerle que ya se está aplicando sería
    cómodo y falso, porque el bucle puede estar dentro de una llamada al modelo.
    """
    tablero = GoalBoard("A")
    assert tablero.pending is None, "el objetivo inicial no es una revisión pendiente"

    tablero.revise("B", base_revision=1)
    assert tablero.pending is not None

    recogido = tablero.take()
    assert recogido is not None and recogido.text == "B"
    assert tablero.pending is None, "recogerlo dos veces lo anunciaría dos veces"
    assert tablero.take() is None


def test_al_modelo_se_le_dice_que_lo_anterior_deja_de_valer() -> None:
    """Añadirle sólo la instrucción nueva le haría hacer las dos.

    La vieja sigue en su transcripción y nada le habría dicho que dejara de aplicarse.
    """
    texto = announcement(Goal("Arregla el login"), Goal("Arregla el registro", revision=2))

    assert "Arregla el registro" in texto
    assert "no longer applies" in texto
    assert "Arregla el login" in texto, "sin el anterior no sabe qué deja de hacer"


# -- lo que una revisión invalida --------------------------------------------


def _loop(
    root: Path, respuestas: list[ModelResponse], tablero: GoalBoard
) -> tuple[AgentLoop, list[RuntimeEvent]]:
    bus = InMemoryEventBus()
    eventos: list[RuntimeEvent] = []
    bus.subscribe(eventos.append)
    registry = ToolRegistry(repository_read_tools())
    loop = AgentLoop(
        FakeModelProvider(respuestas),
        registry,
        ToolExecutor(
            registry,
            PolicyPermissionEngine(PermissionPolicy()),
            InMemoryToolResultStore(),
            bus,
        ),
        ContextBuilder(Workspace.from_path(root)),
        bus,
        verification=LoopCompletionVerificationPolicy(),
        config=AgentLoopConfig(max_iterations=4, session_timeout_seconds=60.0),
    )
    del tablero
    return loop, eventos


def test_el_bucle_recoge_la_revision_y_lo_dice(tmp_path: Path) -> None:
    """Y dice además qué objetivo queda anulado, no sólo cuál empieza."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    tablero = GoalBoard("Cuenta las lineas de a.py")

    async def scenario() -> None:
        # La primera vuelta pide una tool; entre esa y la siguiente llega la revisión.
        loop, eventos = _loop(
            tmp_path,
            [
                ModelResponse(
                    "",
                    "scripted",
                    "tool_calls",
                    tool_calls=(ModelToolCall("c1", "glob", {"pattern": "*.py"}),),
                ),
                ModelResponse("Hecho.", "scripted", "stop"),
            ],
            tablero,
        )

        async def revisar() -> None:
            await asyncio.sleep(0)
            tablero.revise("Cuenta los ficheros", base_revision=1, reason="me explique mal")

        _, resultado = await asyncio.gather(
            revisar(),
            loop.run(
                tablero.current.text,
                Workspace.from_path(tmp_path),
                CancellationSource().token,
                goal=tablero,
            ),
        )

        assert resultado.status is AgentRunStatus.COMPLETED
        (revision,) = [e for e in eventos if e.name is EventName.GOAL_REVISED]
        assert revision.payload["revision"] == 2
        assert revision.payload["supersedes"] == 1
        assert revision.payload["objective"] == "Cuenta los ficheros"
        assert revision.payload["superseded_objective"] == "Cuenta las lineas de a.py"
        assert revision.payload["reason"] == "me explique mal"

    asyncio.run(scenario())


def test_sin_tablero_el_objetivo_es_el_que_llego_y_no_cambia(tmp_path: Path) -> None:
    """Introducir revisiones no puede cambiar lo que hace quien no las usa."""

    async def scenario() -> None:
        loop, eventos = _loop(
            tmp_path, [ModelResponse("Hecho.", "scripted", "stop")], GoalBoard("x")
        )

        resultado = await loop.run(
            "No cambies nada", Workspace.from_path(tmp_path), CancellationSource().token
        )

        assert resultado.status is AgentRunStatus.COMPLETED
        assert not [e for e in eventos if e.name is EventName.GOAL_REVISED]

    asyncio.run(scenario())


def test_la_revision_llega_entre_iteraciones_y_no_a_media_tool(tmp_path: Path) -> None:
    """Un objetivo que cambiase con una tool a medias dejaría al modelo con un resultado
    pedido por un encargo y una pregunta hecha por otro."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    tablero = GoalBoard("Mira a.py")

    async def scenario() -> None:
        loop, eventos = _loop(
            tmp_path,
            [
                ModelResponse(
                    "",
                    "scripted",
                    "tool_calls",
                    tool_calls=(ModelToolCall("c1", "glob", {"pattern": "*.py"}),),
                ),
                ModelResponse("Hecho.", "scripted", "stop"),
            ],
            tablero,
        )
        tablero.revise("Mira otra cosa", base_revision=1)

        await loop.run(
            "Mira a.py",
            Workspace.from_path(tmp_path),
            CancellationSource().token,
            goal=tablero,
        )

        nombres = [e.name for e in eventos]
        revision = nombres.index(EventName.GOAL_REVISED)
        primera_tool = nombres.index(EventName.TOOL_STARTED)
        assert revision < primera_tool, (
            "la revisión pendiente se recoge al empezar la vuelta, antes de pedir nada"
        )

    asyncio.run(scenario())


def test_una_revision_cambia_lo_que_el_run_cree_estar_haciendo(tmp_path: Path) -> None:
    """El estado operativo tiene que seguir al encargo, no al que llego primero.

    Es de donde lee la verificacion, el recovery y quien mire el run despues. Un estado
    que siguiera diciendo el objetivo viejo haria que todos ellos juzgasen el trabajo
    contra algo que ya nadie pedia.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    tablero = GoalBoard("Cuenta las lineas")

    async def scenario() -> None:
        loop, _ = _loop(
            tmp_path,
            [
                ModelResponse(
                    "",
                    "scripted",
                    "tool_calls",
                    tool_calls=(ModelToolCall("c1", "glob", {"pattern": "*.py"}),),
                ),
                ModelResponse("Hecho.", "scripted", "stop"),
            ],
            tablero,
        )
        tablero.revise("Cuenta los ficheros", base_revision=1)

        resultado = await loop.run(
            "Cuenta las lineas",
            Workspace.from_path(tmp_path),
            CancellationSource().token,
            goal=tablero,
        )

        estado = resultado.working_state
        assert estado is not None
        assert estado.objective == "Cuenta los ficheros"
        assert any("revision 2" in decision for decision in estado.decisions), (
            "el cambio de encargo tiene que constar en el estado, no solo en un evento"
        )

    asyncio.run(scenario())
