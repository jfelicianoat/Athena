"""Deshacer lo que hizo un run, y sólo eso.

`checkpoints.py` sabía copiar y restaurar desde H2 y no lo llamaba nadie. `rollback.py`
—el módulo que decide cuándo vale la pena copiar y qué se puede deshacer— existía entero y
no lo importaba ningún otro fichero. Dos capas completas, argumentadas, probadas, y una
tarea que rompía el workspace lo dejaba roto.

Lo que se prueba aquí es la mitad importante: **qué se niega a tocar un rollback**. Uno que
revirtiera el workspace entero se llevaría por delante el trabajo sin commitear de una
persona junto con el error del agente, y esa persona no tendría forma de enterarse.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from athena.checkpoints import CheckpointStore
from athena.rollback import (
    RollbackLedger,
    RollbackScope,
    checkpointing_hook,
    is_worth_checkpointing,
)
from athena.workspace import Workspace


def _sitio(tmp_path: Path) -> tuple[Workspace, RollbackLedger]:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    return (
        Workspace.from_path(root),
        # Fuera del workspace: una copia guardada dentro de lo que protege desaparece con
        # ello, que es la forma más silenciosa de no tener copia.
        RollbackLedger(CheckpointStore(tmp_path / "checkpoints")),
    )


def test_un_rollback_no_toca_lo_que_este_run_no_escribio(tmp_path: Path) -> None:
    """El trabajo sin commitear de una persona no es un daño colateral aceptable.

    Y no tendría forma de enterarse: no hay evento, no hay diff, no hay nada. Por eso el
    alcance no es «el workspace» sino «los ficheros que este run escribió».
    """
    workspace, libro = _sitio(tmp_path)
    mio = workspace.root / "agente.py"
    tuyo = workspace.root / "tuyo.py"
    mio.write_text("original\n", encoding="utf-8")
    tuyo.write_text("lo estaba escribiendo yo\n", encoding="utf-8")

    async def scenario() -> None:
        await libro.checkpoint("t1", workspace, ["agente.py", "tuyo.py"])
        mio.write_text("lo cambio el agente\n", encoding="utf-8")
        tuyo.write_text("y esto lo cambie yo despues\n", encoding="utf-8")
        # El run declara haber escrito sólo el suyo.
        libro.record_written("t1", ["agente.py"])

        resultado = await libro.roll_back(workspace, scope=RollbackScope.RUN)

        assert mio.read_text(encoding="utf-8") == "original\n"
        assert tuyo.read_text(encoding="utf-8") == "y esto lo cambie yo despues\n", (
            "el rollback se llevo por delante trabajo que el run no habia escrito"
        )
        assert "tuyo.py" in resultado.protected

    asyncio.run(scenario())


def test_deshacer_una_tarea_no_deshace_la_de_al_lado(tmp_path: Path) -> None:
    """El alcance es el de la cancelación, y por el mismo motivo."""
    workspace, libro = _sitio(tmp_path)
    uno = workspace.root / "uno.py"
    dos = workspace.root / "dos.py"
    uno.write_text("uno original\n", encoding="utf-8")
    dos.write_text("dos original\n", encoding="utf-8")

    async def scenario() -> None:
        await libro.checkpoint("t1", workspace, ["uno.py"])
        uno.write_text("uno tocado\n", encoding="utf-8")
        libro.record_written("t1", ["uno.py"])
        await libro.checkpoint("t2", workspace, ["dos.py"])
        dos.write_text("dos tocado\n", encoding="utf-8")
        libro.record_written("t2", ["dos.py"])

        await libro.roll_back(workspace, task_id="t1", scope=RollbackScope.TASK)

        assert uno.read_text(encoding="utf-8") == "uno original\n"
        assert dos.read_text(encoding="utf-8") == "dos tocado\n"

    asyncio.run(scenario())


def test_no_se_copia_nada_antes_de_alguien_que_no_puede_escribir() -> None:
    """Un checkpoint antes de un explorer cuesta una copia para protegerse de nada."""
    assert not is_worth_checkpointing(["a.py"], writes=False)
    assert is_worth_checkpointing(["a.py"], writes=True)
    assert not is_worth_checkpointing([], writes=True), "sin ficheros no hay nada que copiar"


def test_nada_del_runtime_deshace_por_su_cuenta() -> None:
    """Un rollback automático tiraría trabajo que una persona podría querer mirar.

    Estructural a propósito: el runtime deja el material —copia antes de editar y anota lo
    escrito— y `roll_back` sólo se llama desde el endpoint HTTP, que es donde hay una
    persona pidiéndolo.
    """
    import athena

    raiz = Path(athena.__file__).parent
    culpables = [
        str(fichero.relative_to(raiz))
        for fichero in raiz.rglob("*.py")
        if fichero.name not in ("rollback.py", "server.py")
        and "roll_back(" in fichero.read_text(encoding="utf-8")
    ]

    assert culpables == [], f"algo deshace sin que nadie lo pida: {culpables}"


def test_la_copia_se_toma_justo_antes_de_editar(tmp_path: Path) -> None:
    """Y no al empezar una tarea, que es donde la puse primero y no servía.

    Un plan real casi nunca nombra los ficheros que va a tocar, así que copiar «lo que la
    tarea declaró» dejaba sin copia justo los runs que el modelo condujo por su cuenta. Lo
    encontró un run contra el broker: arregló un bug y no dejó un solo punto al que volver.
    """
    from athena.hooks import HookContext, HookEvent, HookRegistry

    workspace, libro = _sitio(tmp_path)
    fichero = workspace.root / "calc.py"
    fichero.write_text("original\n", encoding="utf-8")
    gancho = checkpointing_hook(libro, workspace)

    async def scenario() -> None:
        assert gancho.event is HookEvent.PRE_EDIT
        assert not gancho.blocking, "una copia que falla no puede impedir trabajar"

        # Por el registro y no llamando al handler: es el camino que recorre de verdad.
        await HookRegistry((gancho,)).run(
            HookContext(HookEvent.PRE_EDIT, "run-1", {"resources": [str(fichero)]})
        )
        fichero.write_text("tocado\n", encoding="utf-8")

        resultado = await libro.roll_back(workspace, scope=RollbackScope.RUN)

        assert fichero.read_text(encoding="utf-8") == "original\n"
        assert resultado.changed_anything

    asyncio.run(scenario())


def test_un_fichero_fuera_del_workspace_no_se_copia(tmp_path: Path) -> None:
    """El workspace sigue siendo el límite, también para las copias de seguridad."""
    from athena.hooks import HookContext, HookEvent, HookRegistry

    workspace, libro = _sitio(tmp_path)
    fuera = tmp_path / "fuera.txt"
    fuera.write_text("ajeno\n", encoding="utf-8")

    async def scenario() -> None:
        await HookRegistry((checkpointing_hook(libro, workspace),)).run(
            HookContext(HookEvent.PRE_EDIT, "run-1", {"resources": [str(fuera)]})
        )

        assert libro.points() == ()

    asyncio.run(scenario())


def test_el_ejecutor_de_grafos_deja_el_material_para_deshacer(tmp_path: Path) -> None:
    """Antes de escribir se copia, y despues se anota lo escrito de verdad.

    Lo anotado es lo que la tarea escribio, no lo que se penso que tocaria: es la base de
    la unica promesa que hace un rollback, y una lista de intenciones no la sostendria.
    """
    from athena.adapters.service.orchestration import OrchestrationSettings, Orchestrator
    from athena.events import InMemoryEventBus
    from athena.session_store import SqliteSessionStore
    from athena.stores import InMemoryToolResultStore
    from athena.testing import FakeModelProvider

    orquestador = Orchestrator(
        FakeModelProvider([]),
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "sessions.db"),
        InMemoryToolResultStore(),
        OrchestrationSettings(checkpoints=CheckpointStore(tmp_path / "cp")),
    )

    primero = orquestador.ledger_for("run-1")
    otra_vez = orquestador.ledger_for("run-1")
    ajeno = orquestador.ledger_for("run-2")

    assert primero is otra_vez, "dos libros para un run no sabrian donde acaba el run"
    assert primero is not ajeno, "un libro compartido deshacria runs ajenos"


def test_sin_almacen_de_copias_no_hay_libro(tmp_path: Path) -> None:
    """Un despliegue que no quiere copias no las paga, y lo dice en vez de fingir."""
    from athena.adapters.service.orchestration import OrchestrationSettings, Orchestrator
    from athena.events import InMemoryEventBus
    from athena.session_store import SqliteSessionStore
    from athena.stores import InMemoryToolResultStore
    from athena.testing import FakeModelProvider

    orquestador = Orchestrator(
        FakeModelProvider([]),
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "sessions.db"),
        InMemoryToolResultStore(),
        OrchestrationSettings(),
    )

    assert orquestador.ledger_for("run-1") is None
