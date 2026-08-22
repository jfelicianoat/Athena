"""Qué se guarda de un run, en qué orden y a nombre de quién.

Las pruebas son hostiles a propósito: un registro de procedencia sólo vale si sigue
diciendo la verdad cuando el proceso se reinicia, cuando el hijo habla antes que el padre
y cuando una fila del disco está rota.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from athena.events import EventName, RuntimeEvent
from athena.run_event_log import (
    DURABLE,
    ROOT_ACTOR,
    Provenance,
    RunEvent,
    RunEventLog,
    RunEventLogError,
    replay,
)
from athena.types import JSONObject

RUN = "run-1"
CHILD = "child-9"


def _event(
    name: EventName,
    session_id: str = RUN,
    payload: JSONObject | None = None,
    correlation_id: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(name, session_id, payload or {}, correlation_id)


def _log(tmp_path: Path, name: str = "events.db") -> RunEventLog:
    return RunEventLog(tmp_path / name)


def test_a_log_in_memory_is_refused_instead_of_silently_empty(tmp_path: Path) -> None:
    """Cada operación abre su propia conexión, así que `:memory:` no guardaría nada.

    Aceptarlo daría un log que traga escrituras y devuelve una lista vacía, y una lista
    vacía se lee como «no pasó nada». Es la peor forma de fallar: parece un dato.
    """
    del tmp_path
    with pytest.raises(RunEventLogError):
        RunEventLog(":memory:")


def test_only_facts_worth_explaining_tomorrow_are_kept(tmp_path: Path) -> None:
    """El progreso de una tool describe el camino; el log guarda el resultado."""

    async def scenario() -> None:
        log = _log(tmp_path)
        assert await log.record(_event(EventName.TOOL_PROGRESS)) is None
        assert await log.record(_event(EventName.MODEL_STARTED)) is None
        assert await log.record(_event(EventName.TOOL_COMPLETED)) is not None
        assert EventName.TOOL_PROGRESS not in DURABLE

    asyncio.run(scenario())


def test_the_log_numbers_the_facts_and_each_run_has_its_own_order(tmp_path: Path) -> None:
    """`seq` lo asigna el log. Dos runs concurrentes no comparten numeración."""

    async def scenario() -> None:
        log = _log(tmp_path)
        primero = await log.record(_event(EventName.AGENT_STARTED))
        segundo = await log.record(_event(EventName.TOOL_COMPLETED))
        ajeno = await log.record(_event(EventName.AGENT_STARTED, "run-2"))

        assert primero is not None and segundo is not None and ajeno is not None
        assert (primero.seq, segundo.seq) == (1, 2)
        assert ajeno.seq == 1, "la numeración de un run no puede depender de otro"

    asyncio.run(scenario())


def test_reopening_the_log_continues_the_numbering(tmp_path: Path) -> None:
    """Tras un reinicio, los hechos nuevos se ponen detrás, no encima.

    Volver a empezar por 1 no perdería filas: las sobreescribiría por clave primaria, que
    es perder historia sin que nada lo denuncie.
    """

    async def scenario() -> None:
        primera_vida = _log(tmp_path)
        await primera_vida.record(_event(EventName.AGENT_STARTED))
        await primera_vida.record(_event(EventName.TOOL_COMPLETED))

        segunda_vida = _log(tmp_path)
        ultimo = await segunda_vida.record(_event(EventName.AGENT_COMPLETED))

        assert ultimo is not None and ultimo.seq == 3
        assert len(await segunda_vida.read(RUN)) == 3

    asyncio.run(scenario())


def test_what_a_delegate_did_belongs_to_the_run_that_delegated(tmp_path: Path) -> None:
    """Un hijo publica con su propia sesión; su trabajo es del run igualmente.

    Sin esto, la historia de un run jerárquico quedaría partida en tantos trozos como
    agentes intervinieron, y ninguno diría a qué pertenece.
    """

    async def scenario() -> None:
        log = _log(tmp_path)
        await log.record(_event(EventName.AGENT_STARTED))
        await log.record(
            _event(
                EventName.SUBAGENT_STARTED,
                RUN,
                {"role": "explorer", "session_id": CHILD},
                CHILD,
            )
        )
        # El hijo habla por su cuenta, sin decir de quién es.
        await log.record(_event(EventName.TOOL_COMPLETED, CHILD, {"tool": "grep"}))

        historia = await log.read(RUN)
        nombres = [item.name for item in historia]
        assert "tool.completed" in nombres, "el trabajo del delegado se perdió del run"
        delegado = next(item for item in historia if item.name == "tool.completed")
        assert delegado.actor == "explorer", "el hecho no dice quién lo hizo"
        assert delegado.provenance.delegated
        assert await log.read(CHILD) == (), "el hijo no es un run aparte"

    asyncio.run(scenario())


def test_a_delegate_inside_a_task_inherits_the_task(tmp_path: Path) -> None:
    """Y se puede preguntar qué pasó dentro de una tarea concreta."""

    async def scenario() -> None:
        log = _log(tmp_path)
        await log.record(
            _event(
                EventName.TASK_STARTED,
                RUN,
                {"task_id": "t1", "role": "coder"},
                "t1",
            )
        )
        # El ejecutor de grafos nombra al padre del delegado con el id de la tarea.
        await log.record(
            _event(
                EventName.SUBAGENT_STARTED,
                "t1",
                {"role": "coder", "session_id": CHILD},
                CHILD,
            )
        )
        await log.record(_event(EventName.TOOL_COMPLETED, CHILD, {"tool": "edit_file"}))
        await log.record(_event(EventName.TOOL_COMPLETED, RUN, {"tool": "read_file"}))

        de_la_tarea = await log.read_task(RUN, "t1")
        herramientas = [
            item.payload.get("tool") for item in de_la_tarea if item.name == "tool.completed"
        ]
        assert herramientas == ["edit_file"], "la tarea se atribuyó lo que no hizo"
        assert all(item.task_id == "t1" for item in de_la_tarea)

    asyncio.run(scenario())


def test_an_unannounced_child_is_its_own_root_instead_of_a_guess(tmp_path: Path) -> None:
    """Si nadie dijo de quién es una sesión, el log no la adopta.

    Colgarla del último run visto sería una atribución inventada, y una atribución
    inventada es peor que una ausente: se lee igual que una comprobada.
    """

    async def scenario() -> None:
        log = _log(tmp_path)
        await log.record(_event(EventName.AGENT_STARTED))
        huerfano = await log.record(_event(EventName.TOOL_COMPLETED, "sesion-suelta"))

        assert huerfano is not None
        assert huerfano.run_id == "sesion-suelta"
        assert huerfano.actor == ROOT_ACTOR
        assert not huerfano.provenance.delegated
        assert len(await log.read(RUN)) == 1

    asyncio.run(scenario())


def test_lineage_survives_a_restart(tmp_path: Path) -> None:
    """El padre se anunció antes del reinicio; el hijo sigue siendo suyo después."""

    async def scenario() -> None:
        primera_vida = _log(tmp_path)
        await primera_vida.record(
            _event(
                EventName.SUBAGENT_STARTED,
                RUN,
                {"role": "verifier", "session_id": CHILD},
                CHILD,
            )
        )

        segunda_vida = _log(tmp_path)
        tardio = await segunda_vida.record(_event(EventName.TOOL_COMPLETED, CHILD))

        assert tardio is not None
        assert tardio.run_id == RUN, "el reinicio le quitó el padre al delegado"
        assert tardio.actor == "verifier"

    asyncio.run(scenario())


def test_the_bus_identity_is_kept_so_a_stream_can_be_reconciled(tmp_path: Path) -> None:
    """El mismo hecho visto en vivo y leído después tiene que ser uno, no dos."""

    async def scenario() -> None:
        log = _log(tmp_path)
        original = _event(EventName.AGENT_COMPLETED)
        await log.record(original)

        (guardado,) = await log.read(RUN)
        assert guardado.event_id == original.event_id
        assert guardado.occurred_at == original.occurred_at

    asyncio.run(scenario())


def test_a_broken_row_does_not_take_the_rest_down_with_it(tmp_path: Path) -> None:
    """Una fila ilegible conserva su sitio y se declara ilegible."""

    async def scenario() -> None:
        log = _log(tmp_path)
        await log.record(_event(EventName.AGENT_STARTED))
        await log.record(_event(EventName.TOOL_COMPLETED, RUN, {"tool": "grep"}))
        await log.record(_event(EventName.AGENT_COMPLETED))

        with sqlite3.connect(log.database) as connection:
            connection.execute("UPDATE run_events SET payload = ? WHERE seq = 2", ("{roto",))

        historia = await log.read(RUN)
        assert len(historia) == 3, "una fila rota se llevó por delante a las sanas"
        assert historia[1].payload == {"unreadable": True}

    asyncio.run(scenario())


def test_reading_from_a_cursor_returns_only_what_is_new(tmp_path: Path) -> None:
    async def scenario() -> None:
        log = _log(tmp_path)
        for _ in range(3):
            await log.record(_event(EventName.TOOL_COMPLETED))

        assert [item.seq for item in await log.read(RUN, after=1)] == [2, 3]

    asyncio.run(scenario())


def _durable_event(name: str, **kwargs: object) -> RunEvent:
    provenance = kwargs.pop("provenance", Provenance(RUN, RUN))
    assert isinstance(provenance, Provenance)
    payload = kwargs.pop("payload", {})
    assert isinstance(payload, dict)
    seq = kwargs.pop("seq", 1)
    assert isinstance(seq, int)
    correlation_id = kwargs.pop("correlation_id", None)
    assert correlation_id is None or isinstance(correlation_id, str)
    assert not kwargs
    return RunEvent(
        provenance=provenance,
        seq=seq,
        name=name,
        payload=payload,
        correlation_id=correlation_id,
    )


def test_replay_says_what_can_be_asserted_from_the_facts_alone() -> None:
    resumen = replay(
        [
            _durable_event("agent.started", seq=1),
            _durable_event("plan.decided", seq=2, payload={"executed_as": "hierarchical"}),
            _durable_event("task.started", seq=3, payload={"task_id": "t1"}),
            _durable_event("task.completed", seq=4, correlation_id="t1"),
            _durable_event("permission.requested", seq=5),
            _durable_event("verification.completed", seq=6, payload={"status": "passed"}),
            _durable_event("agent.completed", seq=7),
        ]
    )

    assert resumen["status"] == "completed"
    assert resumen["executed_as"] == "hierarchical"
    assert resumen["tasks"] == {"t1": "completed"}
    assert resumen["verification"] == "passed"
    assert resumen["permission_requests"] == 1


def test_a_verification_that_failed_is_not_a_verification_that_never_ran() -> None:
    """Un campo vacio se lee como «no se verifico», y eso seria mentira.

    Lo destapo un run real: la verificacion se pronuncio, dijo que no podia concluir, y el
    resumen lo enseñaba en blanco — indistinguible de un run al que nadie comprobo.
    """
    resumen = replay(
        [
            _durable_event("agent.started", seq=1),
            _durable_event("verification.failed", seq=2, payload={"status": "inconclusive"}),
            _durable_event("agent.failed", seq=3),
        ]
    )

    assert resumen["verification"] == "inconclusive"
    assert resumen["status"] == "failed"


def test_a_task_is_not_a_delegate_just_because_it_is_not_the_root() -> None:
    """Los delegados constan porque alguien los anuncio, no por descarte.

    El ejecutor de grafos publica el ciclo de vida del hijo desde el ambito de la tarea,
    asi que deducir «delegado» de «no viene de la raiz» inventaba un agente por tarea.
    """
    ambito_de_tarea = Provenance(RUN, "t1", actor="coder", task_id="t1")
    resumen = replay(
        [
            _durable_event("agent.started", seq=1),
            _durable_event(
                "subagent.started",
                seq=2,
                provenance=ambito_de_tarea,
                payload={"role": "coder", "session_id": CHILD},
            ),
            _durable_event(
                "tool.completed",
                seq=3,
                provenance=Provenance(RUN, CHILD, actor="coder", task_id="t1"),
            ),
        ]
    )

    assert resumen["delegates"] == {CHILD: "coder"}, "la tarea se conto como un agente"


def test_a_delegate_that_failed_is_not_a_run_that_failed() -> None:
    """El estado del run lo dice la raíz.

    Un hijo puede fallar y el padre reintentar la tarea o darla por prescindible. Creerle
    al hijo daría por terminado en falso un run que seguía trabajando.
    """
    hijo = Provenance(RUN, CHILD, actor="explorer")
    resumen = replay(
        [
            _durable_event("agent.started", seq=1),
            _durable_event(
                "subagent.started",
                seq=2,
                payload={"role": "explorer", "session_id": CHILD},
            ),
            _durable_event("agent.failed", seq=3, provenance=hijo),
        ]
    )

    assert resumen["status"] == "running"
    assert resumen["delegates"] == {CHILD: "explorer"}


def test_a_run_nobody_recorded_reads_as_empty_not_as_finished(tmp_path: Path) -> None:
    """Y el resumen de una historia vacía no afirma nada."""

    async def scenario() -> None:
        log = _log(tmp_path)
        assert await log.read("no-existe") == ()
        assert replay(())["status"] == "unknown"

    asyncio.run(scenario())


def test_the_payload_survives_a_round_trip_with_accents(tmp_path: Path) -> None:
    """El log guarda texto, no bytes de una codificación concreta."""

    async def scenario() -> None:
        log = _log(tmp_path)
        await log.record(_event(EventName.TOOL_FAILED, RUN, {"message": "no se pudo leer ñ"}))

        (guardado,) = await log.read(RUN)
        assert guardado.payload["message"] == "no se pudo leer ñ"
        assert json.dumps(guardado.to_json())

    asyncio.run(scenario())


def test_un_cambio_de_encargo_sobrevive_al_proceso(tmp_path: Path) -> None:
    """Un run que acabo haciendo otra cosa solo es explicable si consta que cambio.

    Se descubrio en un run real: la revision se aplicaba, se publicaba y no quedaba en el
    registro, asi que la historia guardada contaba un trabajo que no encajaba con el
    objetivo con el que empezaba y nada decia por que.
    """

    async def scenario() -> None:
        log = _log(tmp_path)
        await log.record(_event(EventName.AGENT_STARTED))
        await log.record(
            _event(
                EventName.GOAL_REVISED,
                RUN,
                {"revision": 2, "supersedes": 1, "objective": "otra cosa"},
            )
        )

        nombres = [item.name for item in await log.read(RUN)]
        assert "goal.revised" in nombres

    asyncio.run(scenario())
