"""Un registro append-only de lo que pasó de verdad en un run, y de quién lo hizo.

`EventBus` es en vivo: quien no estaba escuchando no se entera, y eso es correcto para lo
que hace —avisar a interfaces— y no sirve para responder «qué ocurrió» después de un
reinicio. `SessionStore` guarda estado, que es la foto y no la película.

Esto es lo tercero: hechos, en orden, que sobreviven al proceso. No sustituye a ninguno de
los dos ni convierte a Athena en event sourcing; el estado sigue siendo la fuente para
saber dónde está un run, y esto sirve para saber cómo llegó.

Se guarda poco a propósito. Un log que registrase cada trozo de stream crecería con la
verbosidad del modelo en vez de con lo que hizo el agente, y el coste de escribirlo lo
pagaría cada run. Lo que entra son decisiones y resultados: lo que alguien necesitaría para
reconstruir el run, no para revivirlo.

## Procedencia

Un hecho sin autor no explica nada. Un run con delegados publica por el mismo bus desde
varias sesiones —la del run, la de cada hijo— y guardarlos por `session_id` partiría la
historia de un run en tantos trozos como agentes intervinieron, cada uno mudo sobre a qué
pertenece. Así que cada fila lleva, además del hecho: el run **raíz** al que pertenece, la
sesión que lo publicó, la tarea dentro de la que ocurrió si ocurrió dentro de una, y el
papel de quien actuaba.

El linaje se aprende de los propios eventos, no se configura: `task.started` dice qué
tarea pertenece a qué run, y `subagent.started` dice qué sesión hija pertenece a qué
padre. Por eso un delegado debe anunciarse **al empezar** y con su nombre: si sólo se
supiera al terminar, todo lo que hizo mientras tanto llegaría aquí huérfano.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from athena.errors import AthenaRuntimeError
from athena.events import EventName, RuntimeEvent
from athena.types import JSONObject

#: Lo que merece sobrevivir al proceso.
#:
#: Deliberadamente no está todo. `model.started`, `tool.progress` y las deltas de stream
#: describen el camino y no el resultado; guardarlas haría el log proporcional a lo
#: hablador que sea el modelo. Un hecho entra aquí si alguien tendría que saberlo para
#: explicar el run mañana.
DURABLE: frozenset[EventName] = frozenset(
    {
        EventName.AGENT_STARTED,
        EventName.AGENT_COMPLETED,
        EventName.AGENT_FAILED,
        EventName.AGENT_CANCELLED,
        # Un run que acabo haciendo otra cosa de la que se le pidio solo es explicable
        # si consta cuando cambio el encargo y por que. Sin esto, el registro cuenta un
        # trabajo que no encaja con el objetivo con el que empezo y nada dice por que.
        EventName.GOAL_REVISED,
        EventName.PLAN_DECIDED,
        EventName.GRAPH_STARTED,
        EventName.GRAPH_COMPLETED,
        EventName.GRAPH_FAILED,
        EventName.GRAPH_CANCELLED,
        EventName.TASK_STARTED,
        EventName.TASK_COMPLETED,
        EventName.TASK_FAILED,
        EventName.TASK_BLOCKED,
        # Los subagentes entran por dos motivos, no por uno: son hechos del run, y son
        # además lo que permite atribuir todo lo que publique el hijo. Quitarlos de aquí
        # no ahorraría filas, dejaría sin autor a las que quedasen.
        EventName.SUBAGENT_STARTED,
        EventName.SUBAGENT_CONTINUED,
        EventName.SUBAGENT_COMPLETED,
        EventName.SUBAGENT_FAILED,
        EventName.SUBAGENT_CANCELLED,
        EventName.MODEL_COMPLETED,
        EventName.MODEL_FAILED,
        EventName.TOOL_COMPLETED,
        EventName.TOOL_FAILED,
        EventName.PERMISSION_REQUESTED,
        EventName.PERMISSION_RESOLVED,
        EventName.VERIFICATION_COMPLETED,
        EventName.VERIFICATION_FAILED,
        EventName.CAPABILITY_MISSING,
        EventName.RECOVERY_ACTION,
        EventName.SESSION_PERSISTED,
    }
)

#: El papel de quien actúa cuando no actúa ningún delegado: el propio run.
ROOT_ACTOR = "run"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT,
    actor TEXT NOT NULL,
    correlation_id TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS run_lineage (
    session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT,
    actor TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS run_events_by_task ON run_events (run_id, task_id, seq);
"""

#: Versión del formato de un hecho. Va en cada fila y no en el fichero: un log que
#: sobrevive a varias versiones de Athena contiene filas de varias, y decidir cómo leer
#: cada una exige saber cuál es cuál.
EVENT_VERSION = 1


@dataclass(frozen=True, slots=True)
class Provenance:
    """De quién es un hecho.

    `run_id` es la raíz, no la sesión que lo publicó: es lo que hace que un run con
    delegados tenga una sola historia en vez de una por agente.
    """

    run_id: str
    session_id: str
    actor: str = ROOT_ACTOR
    task_id: str | None = None

    @property
    def delegated(self) -> bool:
        """Si lo hizo un delegado y no el propio run."""
        return self.session_id != self.run_id

    def to_json(self) -> JSONObject:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "actor": self.actor,
            "task_id": self.task_id,
            "delegated": self.delegated,
        }


@dataclass(frozen=True, slots=True)
class RunEvent:
    """Un hecho, con su sitio en el orden y su autor."""

    provenance: Provenance
    seq: int
    name: str
    payload: JSONObject = field(default_factory=dict)
    correlation_id: str | None = None
    #: La identidad que tuvo en el bus. Sin ella, el mismo hecho visto en vivo y leído
    #: después parecen dos, y nadie puede reconciliar un stream con su registro.
    event_id: str = ""
    version: int = EVENT_VERSION
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def run_id(self) -> str:
        return self.provenance.run_id

    @property
    def task_id(self) -> str | None:
        return self.provenance.task_id

    @property
    def actor(self) -> str:
        return self.provenance.actor

    def to_json(self) -> JSONObject:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "name": self.name,
            "version": self.version,
            "correlation_id": self.correlation_id,
            "occurred_at": self.occurred_at.isoformat(),
            "provenance": self.provenance.to_json(),
            "payload": dict(self.payload),
        }


class RunEventLogError(AthenaRuntimeError):
    """El log no pudo escribirse o leerse."""

    code = "run_event_log_error"


class RunEventLog:
    """Los hechos de cada run, en orden, sin huecos y con autor.

    `seq` es monótono **por run** y lo asigna el log, no quien publica. Dejarlo al emisor
    haría que dos componentes concurrentes eligieran el mismo número y el orden dejaría de
    ser un orden.
    """

    def __init__(self, database: Path | str) -> None:
        if str(database) == ":memory:":
            # Cada operacion abre su propia conexion, asi que una base en memoria seria
            # una distinta y vacia cada vez: el log aceptaria escrituras y no devolveria
            # nada, que es peor que negarse. Un log que miente no es un log.
            raise RunEventLogError("El log de eventos necesita un fichero, no :memory:")
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._next: dict[str, int] = {}
        self._lineage: dict[str, Provenance] = {}
        try:
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise RunEventLogError(f"No se pudo abrir el log de eventos: {exc}") from exc
        self.load_lineage()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    # -- escritura ---------------------------------------------------------

    async def record(self, event: RuntimeEvent) -> RunEvent | None:
        """Guardar un evento del bus si es de los que duran. `None` si no lo es.

        El linaje se aprende aquí dentro, bajo el mismo cerrojo que la escritura: si se
        aprendiera fuera, dos eventos concurrentes podrían resolver su procedencia contra
        un linaje a medio construir y atribuirse al run equivocado.
        """
        if event.name not in DURABLE:
            return None
        async with self._lock:
            try:
                return await asyncio.to_thread(self._record, event)
            except sqlite3.Error as exc:
                raise RunEventLogError(f"No se pudo guardar {event.name.value}: {exc}") from exc

    def _record(self, event: RuntimeEvent) -> RunEvent:
        provenance = self._resolve(event)
        with self._connect() as connection:
            self._learn(connection, event, provenance)
            seq = self._reserve(connection, provenance.run_id)
            record = RunEvent(
                provenance=provenance,
                seq=seq,
                name=event.name.value,
                payload=dict(event.payload),
                correlation_id=event.correlation_id,
                event_id=event.event_id,
                occurred_at=event.occurred_at,
            )
            connection.execute(
                "INSERT INTO run_events (run_id, seq, event_id, occurred_at, name, version, "
                "session_id, task_id, actor, correlation_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.provenance.run_id,
                    record.seq,
                    record.event_id,
                    record.occurred_at.isoformat(),
                    record.name,
                    record.version,
                    record.provenance.session_id,
                    record.provenance.task_id,
                    record.provenance.actor,
                    record.correlation_id,
                    json.dumps(record.payload, ensure_ascii=False),
                ),
            )
        return record

    def _reserve(self, connection: sqlite3.Connection, run_id: str) -> int:
        if run_id not in self._next:
            # Al arrancar sobre un log que ya existe, la numeración continúa donde se
            # quedó. Empezar de cero reescribiría hechos ajenos con los propios.
            row = connection.execute(
                "SELECT MAX(seq) FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            self._next[run_id] = (row[0] or 0) + 1
        seq = self._next[run_id]
        self._next[run_id] = seq + 1
        return seq

    # -- procedencia -------------------------------------------------------

    def _resolve(self, event: RuntimeEvent) -> Provenance:
        """A qué run pertenece un hecho y quién lo hizo.

        Sin linaje conocido, una sesión es su propia raíz. Es la respuesta honesta: un
        run monoagente lo es de verdad, y un hijo cuyo padre no se anunció no puede
        atribuirse por adivinanza a un run que quizá no sea el suyo.
        """
        known = self._lineage.get(event.session_id)
        if known is not None:
            return known
        return Provenance(run_id=event.session_id, session_id=event.session_id)

    def _learn(
        self, connection: sqlite3.Connection, event: RuntimeEvent, provenance: Provenance
    ) -> None:
        """Registrar lo que este evento revela sobre a quién pertenece qué."""
        if event.name is EventName.TASK_STARTED:
            task_id = _text(event.payload.get("task_id")) or _text(event.correlation_id)
            if task_id:
                # La tarea es una sesión más a efectos de linaje: el ejecutor de grafos
                # nombra al padre de un delegado con el id de la tarea, así que lo que el
                # hijo publique llegará colgando de ahí.
                self._remember(
                    connection,
                    task_id,
                    Provenance(
                        run_id=provenance.run_id,
                        session_id=task_id,
                        actor=_text(event.payload.get("role")) or ROOT_ACTOR,
                        task_id=task_id,
                    ),
                )
            return
        if event.name is EventName.SUBAGENT_STARTED:
            child = _text(event.payload.get("session_id")) or _text(event.correlation_id)
            if child:
                self._remember(
                    connection,
                    child,
                    Provenance(
                        run_id=provenance.run_id,
                        session_id=child,
                        actor=_text(event.payload.get("role")) or ROOT_ACTOR,
                        # Un delegado lanzado desde una tarea hereda la tarea; uno lanzado
                        # por la herramienta de delegación no tiene ninguna, y decir que
                        # tiene una sería inventarla.
                        task_id=provenance.task_id,
                    ),
                )

    def _remember(
        self, connection: sqlite3.Connection, session_id: str, provenance: Provenance
    ) -> None:
        self._lineage[session_id] = provenance
        connection.execute(
            "INSERT OR REPLACE INTO run_lineage (session_id, run_id, task_id, actor) "
            "VALUES (?, ?, ?, ?)",
            (session_id, provenance.run_id, provenance.task_id, provenance.actor),
        )

    def load_lineage(self) -> None:
        """Recuperar el linaje escrito por procesos anteriores.

        Hace falta al reabrir un log: sin esto, los hechos de un delegado que empezó antes
        del reinicio se guardarían bajo su propia sesión, y el run perdería un trozo de su
        historia justo por haberse reiniciado.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id, run_id, task_id, actor FROM run_lineage"
            ).fetchall()
        for row in rows:
            session_id = str(row[0])
            self._lineage[session_id] = Provenance(
                run_id=str(row[1]),
                session_id=session_id,
                actor=str(row[3]),
                task_id=None if row[2] is None else str(row[2]),
            )

    # -- lectura -----------------------------------------------------------

    async def read(self, run_id: str, *, after: int = 0) -> tuple[RunEvent, ...]:
        """Los hechos de un run, en orden, desde donde se pida.

        Incluye lo que hicieron sus delegados: son parte de lo que hizo el run, y
        devolverlos aparte obligaría a quien pregunta a reconstruir el árbol para
        entender una respuesta que ya lo tiene.
        """
        return await asyncio.to_thread(self._read, run_id, after, None)

    async def read_task(self, run_id: str, task_id: str) -> tuple[RunEvent, ...]:
        """Lo que ocurrió dentro de una tarea concreta."""
        return await asyncio.to_thread(self._read, run_id, 0, task_id)

    def _read(self, run_id: str, after: int, task_id: str | None) -> tuple[RunEvent, ...]:
        query = (
            "SELECT run_id, seq, event_id, occurred_at, name, version, session_id, "
            "task_id, actor, correlation_id, payload FROM run_events "
            "WHERE run_id = ? AND seq > ?"
        )
        parameters: tuple[object, ...] = (run_id, after)
        if task_id is not None:
            query += " AND task_id = ?"
            parameters += (task_id,)
        query += " ORDER BY seq"
        try:
            with self._connect() as connection:
                rows = connection.execute(query, parameters).fetchall()
        except sqlite3.Error as exc:
            raise RunEventLogError(f"No se pudo leer el run {run_id}: {exc}") from exc
        return tuple(_row_to_event(row) for row in rows)

    async def runs(self) -> tuple[str, ...]:
        return await asyncio.to_thread(self._runs)

    def _runs(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM run_events GROUP BY run_id ORDER BY MIN(seq)"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _row_to_event(row: tuple[object, ...]) -> RunEvent:
    try:
        payload = json.loads(str(row[10]))
    except json.JSONDecodeError:
        # Una fila ilegible no puede tumbar la lectura de las demás: se conserva su sitio
        # en el orden y se dice que no se pudo leer, que es más honesto que omitirla.
        payload = {"unreadable": True}
    return RunEvent(
        provenance=Provenance(
            run_id=str(row[0]),
            session_id=str(row[6]),
            actor=str(row[8]),
            task_id=None if row[7] is None else str(row[7]),
        ),
        seq=int(str(row[1])),
        event_id=str(row[2]),
        occurred_at=datetime.fromisoformat(str(row[3])),
        name=str(row[4]),
        version=int(str(row[5])),
        correlation_id=None if row[9] is None else str(row[9]),
        payload=payload if isinstance(payload, dict) else {"value": payload},
    )


def replay(events: Iterable[RunEvent]) -> JSONObject:
    """Reconstruir lo esencial de un run a partir de sus hechos.

    No devuelve el estado entero: devuelve lo que se puede afirmar mirando sólo el log, que
    es lo que sirve tras un reinicio para saber si hay que recuperar algo y qué.
    """
    status = "unknown"
    shape = ""
    tasks: dict[str, str] = {}
    delegates: dict[str, str] = {}
    verification = ""
    permissions = 0
    for event in events:
        # Sólo la raíz decide el estado del run. Un delegado que falla no es un run que
        # falla: el padre puede reintentar la tarea o darla por prescindible, y creerle al
        # hijo daría por terminado en falso lo que todavía estaba pasando.
        if not event.provenance.delegated:
            if event.name in ("agent.completed", "graph.completed"):
                status = "completed"
            elif event.name in ("agent.failed", "graph.failed"):
                status = "failed"
            elif event.name in ("agent.cancelled", "graph.cancelled"):
                status = "cancelled"
            elif event.name == "agent.started" and status == "unknown":
                status = "running"
            # Tambien la que fallo. Mirar solo `verification.completed` dejaba el campo
            # vacio cuando la verificacion se pronuncio y dijo que no: vacio se lee como
            # «no se verifico», que es justo lo contrario de lo que paso.
            if event.name in ("verification.completed", "verification.failed"):
                result = event.payload.get("status")
                verification = result if isinstance(result, str) else ""
        if event.name == "plan.decided":
            executed = event.payload.get("executed_as")
            shape = executed if isinstance(executed, str) else ""
        if event.name.startswith("task."):
            task_id = _text(event.payload.get("task_id")) or _text(event.correlation_id)
            if task_id:
                tasks[task_id] = event.name.removeprefix("task.")
        if event.name == "permission.requested":
            permissions += 1
        # Un delegado consta porque alguien lo anuncio, no porque se deduzca de que un
        # hecho no venga de la raiz: el ejecutor de grafos publica el ciclo de vida del
        # hijo desde el ambito de la tarea, y darla por delegada convertiria la tarea en
        # un agente que no existe.
        if event.name == "subagent.started":
            child = _text(event.payload.get("session_id")) or _text(event.correlation_id)
            if child:
                delegates[child] = _text(event.payload.get("role")) or ROOT_ACTOR
    return {
        "status": status,
        "executed_as": shape,
        "tasks": tasks,
        "delegates": delegates,
        "verification": verification,
        "permission_requests": permissions,
    }


__all__ = [
    "DURABLE",
    "EVENT_VERSION",
    "ROOT_ACTOR",
    "Provenance",
    "RunEvent",
    "RunEventLog",
    "RunEventLogError",
    "replay",
]
