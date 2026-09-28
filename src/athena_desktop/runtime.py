"""Capa de aplicacion del escritorio: lo que la ventana pide, sin nada de Tk.

Antes el escritorio montaba su propio `AgentLoop` y se quedaba sin la mitad de Athena:
sin perfiles de evidencia, sin memoria de proyecto, sin copias para deshacer, sin
historial ni reanudacion, y con una verificacion propia que daba por bueno un run porque
el modelo habia dejado de hablar (A04, A16). Ahora corre sobre el mismo `RunRegistry` que
el servicio y que ChatyGPT: una sola implementacion de las garantias, tres interfaces.

El estado vive fuera del proyecto, en `%LOCALAPPDATA%\\Athena\\desktop`: una base de datos
de Athena dentro de la carpeta del usuario es un fichero que no pidio, y las copias de
seguridad dentro de lo que protegen desaparecen con ello.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.adapters.openai_compatible import OpenAICompatibleModelProvider
from athena.adapters.service import CapabilityMode as ServiceMode
from athena.adapters.service import RunOptions, RunRegistry
from athena.adapters.service.orchestration import ExecutionMode, OrchestrationSettings
from athena.agent_loop import AgentRunResult
from athena.cancellation import CancellationSource
from athena.checkpoints import CheckpointStore
from athena.errors import AthenaRuntimeError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.goals import Goal
from athena.models import ModelHealthStatus, ModelProvider
from athena.permissions import PermissionDecision, PermissionRequest
from athena.project_memory import SqliteProjectMemory
from athena.rollback import RollbackLedger, RollbackResult, RollbackScope
from athena.run_event_log import RunEventLog
from athena.session_store import SessionRecord, SqliteSessionStore
from athena.stores import SqliteToolResultStore
from athena.types import JSONObject
from athena.workspace import Workspace
from athena_desktop.config import CapabilityMode, ProviderKind

EventCallback = Callable[[RuntimeEvent], None]
PermissionCallback = Callable[[PermissionRequest], PermissionDecision]

#: Que clase de trabajo es. Lo elige la persona; no se adivina del texto (A04).
TaskKind = Literal["question", "change", "documents"]

#: Perfil de Athena para cada clase de trabajo.
_PROFILE_FOR: dict[str, str] = {
    "question": "questions",
    "change": "software_engineering",
    "documents": "documents",
}

#: Cuanto tiene una persona para contestar una aprobacion desde que la ve.
_HUMAN_WINDOW_SECONDS = 300.0

_FILE_VERB_PREFIXES = (
    "añad",
    "crae",
    "crea",
    "corrig",
    "arregl",
    "edit",
    "escrib",
    "genera",
    "guard",
    "modific",
    "cambi",
    "refactor",
    "add",
    "create",
    "fix",
    "generate",
    "modify",
    "save",
    "write",
    "change",
    "update",
)
_FILE_NOUN_PREFIXES = ("archivo", "documento", "fichero", "document", "file")
_FILE_NAME = re.compile(r"[\w.-]+\.[A-Za-z0-9]{1,6}\b")


@dataclass(frozen=True, slots=True)
class RunConfiguration:
    workspace: Path
    objective: str
    provider: ProviderKind
    base_url: str
    model: str = ""
    token: str = ""
    writes: CapabilityMode = "off"
    execution: CapabilityMode = "off"
    max_iterations: int = 12
    timeout_seconds: float = 900.0
    task_kind: TaskKind = "question"
    deliverables: tuple[str, ...] = field(default=())

    def validate(self) -> None:
        # Una ruta vacia es `Path("")`, que es la carpeta actual y pasa `is_dir()`: el
        # lanzador cambia a la carpeta de Athena, asi que un formulario vacio acababa
        # apuntando al codigo de la propia aplicacion (A17). Solo vale una ruta absoluta.
        if not str(self.workspace).strip() or not Path(self.workspace).is_absolute():
            raise ValueError("Elige la carpeta del proyecto con el botón «Elegir…»")
        if not Path(self.workspace).is_dir():
            raise ValueError(f"La carpeta del proyecto no existe: {self.workspace}")
        if not self.objective.strip():
            raise ValueError("Describe qué quieres que haga Athena")
        parsed = urlsplit(self.base_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("La URL del proveedor debe comenzar por http:// o https://")
        if self.provider is ProviderKind.AI_BROKER and not self.token.strip():
            raise ValueError("AI_Broker necesita un token")
        if self.provider is ProviderKind.OPENAI_COMPATIBLE and not self.model.strip():
            raise ValueError("El proveedor OpenAI-compatible necesita un modelo")
        if self.max_iterations <= 0 or self.timeout_seconds <= 0:
            raise ValueError("Los límites de ejecución deben ser mayores que cero")
        if self.task_kind not in _PROFILE_FOR:
            raise ValueError(f"Tipo de tarea desconocido: {self.task_kind}")
        if self.task_kind in ("change", "documents") and self.writes == "off":
            raise ValueError(
                "Para cambiar el proyecto o crear documentos, «Cambios en archivos» tiene "
                "que estar en «Preguntar» o «Permitir»"
            )


def default_state_dir(environment: dict[str, str] | None = None) -> Path:
    """Donde guarda el escritorio sus sesiones, copias e historial. Fuera del proyecto."""
    env = os.environ if environment is None else environment
    local = env.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "Athena" / "desktop"
    return Path.home() / ".athena" / "desktop"


def build_provider(configuration: RunConfiguration) -> ModelProvider:
    configuration.validate()
    if configuration.provider is ProviderKind.AI_BROKER:
        return AiBrokerModelProvider(
            configuration.base_url,
            configuration.token,
            preferred_model=configuration.model.strip() or None,
            request_timeout_seconds=max(30.0, configuration.timeout_seconds),
            max_wait_seconds=max(30.0, configuration.timeout_seconds),
        )
    return OpenAICompatibleModelProvider(
        configuration.base_url,
        configuration.model,
        api_key=configuration.token or None,
    )


def run_options(configuration: RunConfiguration) -> RunOptions:
    """Lo que la persona eligio, dicho en el vocabulario del runtime."""
    writes = ServiceMode(configuration.writes)
    if configuration.task_kind == "question":
        # Una consulta no escribe: su perfil ni siquiera tiene las herramientas.
        writes = ServiceMode.OFF
    return RunOptions(
        writes=writes,
        execution=ServiceMode(configuration.execution),
        max_iterations=configuration.max_iterations,
        session_timeout_seconds=configuration.timeout_seconds,
        execution_mode=ExecutionMode.DIRECT,
        profile=_PROFILE_FOR[configuration.task_kind],
        deliverables=configuration.deliverables,
        require_change=configuration.task_kind == "change",
    )


@dataclass(slots=True)
class DesktopStores:
    """Los almacenes del escritorio, compartidos entre runs."""

    root: Path

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def sessions(self) -> SqliteSessionStore:
        return SqliteSessionStore(self.root / "sessions.db")

    @property
    def checkpoints(self) -> CheckpointStore:
        return CheckpointStore(self.root / "checkpoints")

    @property
    def events(self) -> RunEventLog:
        return RunEventLog(self.root / "events.db")


def build_registry(
    configuration: RunConfiguration, stores: DesktopStores, bus: InMemoryEventBus | None = None
) -> RunRegistry:
    """Un registro con todo lo que el servicio tiene: memoria, copias e historial."""
    return RunRegistry(
        build_provider(configuration),
        bus or InMemoryEventBus(),
        stores.sessions,
        SqliteToolResultStore(stores.root / "results.db"),
        orchestration=OrchestrationSettings(
            memory=SqliteProjectMemory(stores.root / "memory.db"),
            checkpoints=stores.checkpoints,
        ),
        event_log=stores.events,
    )


class RunHandle:
    """Lo que la ventana puede hacer con un run en marcha, desde su propio hilo."""

    def __init__(self, registry: RunRegistry, run_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.registry = registry
        self.run_id = run_id
        self._loop = loop
        self._pending_cancel: object | None = None

    def revise_goal(self, text: str) -> Goal:
        """Cambiar el encargo; se aplica en el siguiente paso del run."""

        async def revise() -> Goal:
            board = self.registry.goal_of(self.run_id)
            return self.registry.revise_goal(
                self.run_id, text, base_revision=board.current.revision
            )

        return asyncio.run_coroutine_threadsafe(revise(), self._loop).result(timeout=10)

    def cancel(self) -> None:
        # Desde cualquier hilo: la ventana cancela desde el suyo.
        self._pending_cancel = asyncio.run_coroutine_threadsafe(
            self.registry.cancel(self.run_id), self._loop
        )


async def run_athena(
    configuration: RunConfiguration,
    cancellation: CancellationSource,
    *,
    on_event: EventCallback,
    on_permission: PermissionCallback,
    state_dir: Path | None = None,
    on_started: Callable[[RunHandle], None] | None = None,
    resume_run_id: str | None = None,
) -> AgentRunResult:
    """Ejecutar (o reanudar) un encargo y devolver su resultado.

    Las aprobaciones llegan como eventos del run, igual que a ChatyGPT, y se contestan
    llamando a `on_permission` desde un hilo aparte: la ventana puede tener el dialogo
    abierto sin parar el bucle de eventos.
    """
    configuration.validate()
    stores = DesktopStores(state_dir or default_state_dir())
    registry = build_registry(configuration, stores)
    loop = asyncio.get_running_loop()
    await registry.mark_interrupted()
    if resume_run_id is not None:
        workspace = await _workspace_of(stores.sessions, resume_run_id)
        run_id = await registry.resume(resume_run_id, workspace)
    else:
        workspace = Workspace.from_path(configuration.workspace)
        run_id = await registry.start(
            configuration.objective.strip(), workspace, run_options(configuration)
        )
    subscriber = registry.subscribe(run_id, control=True)
    # Lo que el run publico antes de que la ventana se suscribiera (su arranque) tambien
    # se cuenta. Sin await entre medias: no hay hueco para que se cuele uno repetido.
    for earlier in tuple(registry.run(run_id).recent):
        on_event(earlier)
    handle = RunHandle(registry, run_id, loop)
    unregister = cancellation.token.register(handle.cancel)
    if cancellation.token.is_cancelled:
        handle.cancel()
    if on_started is not None:
        on_started(handle)
    pump = asyncio.ensure_future(_pump(registry, subscriber.queue, on_event, on_permission))
    try:
        return await registry.wait(run_id)
    finally:
        unregister()
        registry.unsubscribe(subscriber)
        await asyncio.gather(pump, return_exceptions=True)
        await registry.shutdown()


async def _pump(
    registry: RunRegistry,
    queue: asyncio.Queue[RuntimeEvent | None],
    on_event: EventCallback,
    on_permission: PermissionCallback,
) -> None:
    while True:
        event = await queue.get()
        if event is None:
            return
        on_event(event)
        if event.name is EventName.PERMISSION_REQUESTED and event.payload.get("awaiting_decision"):
            request_id = str(event.payload.get("request_id") or event.correlation_id or "")
            pending = registry.approvals.get(request_id)
            if pending is None:
                continue
            # Visto: desde aqui corre el reloj de la persona, no el de entrega.
            registry.approvals.acknowledge(request_id, _HUMAN_WINDOW_SECONDS)
            decision = await asyncio.to_thread(on_permission, pending.request)
            registry.approvals.resolve(request_id, decision)


async def _workspace_of(sessions: SqliteSessionStore, run_id: str) -> Workspace:
    manifest = await sessions.load_manifest(run_id)
    root = None if manifest is None else manifest.get("workspace_root")
    if not isinstance(root, str) or not root:
        raise AthenaRuntimeError(
            "No consta en qué proyecto se creó este trabajo, así que no se puede reanudar "
            "ni deshacer desde aquí"
        )
    return Workspace.from_path(root)


# ------------------------------------------------------------------ historial y deshacer


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    status: str
    objective: str
    project: str
    updated_at: str
    verification: str
    files_modified: tuple[str, ...]
    errors: tuple[str, ...]
    task_kind: str
    resumable: bool
    undoable: int

    @property
    def label(self) -> str:
        return Path(self.project).name or self.project


async def list_runs(state_dir: Path | None = None, *, limit: int = 200) -> list[RunSummary]:
    """Los trabajos que el escritorio recuerda, del mas reciente al mas antiguo."""
    stores = DesktopStores(state_dir or default_state_dir())
    sessions = stores.sessions
    records = await sessions.list_sessions()
    checkpoints = await asyncio.to_thread(stores.checkpoints.list)
    undoable: dict[str, int] = {}
    for checkpoint in checkpoints:
        if checkpoint.rolled_back or not checkpoint.run_id:
            continue
        written = sum(1 for entry in checkpoint.entries if entry.written)
        undoable[checkpoint.run_id] = undoable.get(checkpoint.run_id, 0) + written
    summaries: list[RunSummary] = []
    for record in records[:limit]:
        manifest = await sessions.load_manifest(record.session_id)
        if manifest is None:
            # Sesiones de delegados y tareas internas: no son trabajos de la persona.
            continue
        summaries.append(_summary(record, manifest, undoable.get(record.session_id, 0)))
    return summaries


def _summary(record: SessionRecord, manifest: JSONObject, undoable: int) -> RunSummary:
    working = record.working_memory
    options = manifest.get("options")
    profile = options.get("profile") if isinstance(options, dict) else ""
    kind = next((kind for kind, name in _PROFILE_FOR.items() if name == profile), "change")
    verification = record.verification.get("summary") or working.verification.get("summary")
    return RunSummary(
        run_id=record.session_id,
        status=record.status.value,
        objective=str(manifest.get("objective") or working.objective),
        project=str(manifest.get("workspace_root") or ""),
        updated_at=record.updated_at.astimezone().strftime("%Y-%m-%d %H:%M"),
        verification=str(verification or ""),
        files_modified=tuple(working.files_modified),
        errors=tuple(f"{error.code}: {error.message}" for error in working.errors[-3:]),
        task_kind=kind,
        resumable=record.resumable,
        undoable=undoable,
    )


async def roll_back_run(run_id: str, state_dir: Path | None = None) -> RollbackResult:
    """Deshacer lo que escribio un trabajo, sin tocar lo que otros cambiaron despues."""
    stores = DesktopStores(state_dir or default_state_dir())
    workspace = await _workspace_of(stores.sessions, run_id)
    ledger = RollbackLedger.load(stores.checkpoints, run_id)
    return await ledger.roll_back(workspace, scope=RollbackScope.RUN)


# ------------------------------------------------------------------ probar la conexion


@dataclass(frozen=True, slots=True)
class ConnectionReport:
    ok: bool
    message: str


async def check_connection(configuration: RunConfiguration) -> ConnectionReport:
    """Probar proveedor y credencial sin lanzar ningun encargo (A03)."""
    parsed = urlsplit(configuration.base_url.rstrip("/"))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ConnectionReport(False, "La URL debe comenzar por http:// o https://")
    if configuration.provider is ProviderKind.AI_BROKER and not configuration.token.strip():
        return ConnectionReport(False, "Falta el token de AI_Broker")
    token = CancellationSource().token
    provider = build_provider(configuration)
    if isinstance(provider, AiBrokerModelProvider):
        health = await provider.health(token)
        if health.status is ModelHealthStatus.UNAVAILABLE:
            return ConnectionReport(False, f"El broker no responde: {health.detail}")
        accepted, message = await provider.verify_credentials(token)
        if not accepted:
            return ConnectionReport(False, message)
        estado = "" if health.status is ModelHealthStatus.HEALTHY else " (informa estado degradado)"
        return ConnectionReport(True, f"Conectado a AI_Broker{estado}. {message}")
    health = await provider.health(token)
    if health.status is ModelHealthStatus.HEALTHY:
        return ConnectionReport(True, "El endpoint responde y acepta la credencial.")
    return ConnectionReport(False, f"El endpoint no está disponible: {health.detail}")


def requires_workspace_change(objective: str) -> bool:
    """Si el texto parece pedir cambios en archivos. Solo para avisar, nunca decide.

    La clase de trabajo la elige la persona. Esto sirve para preguntarle si de verdad
    queria una consulta cuando escribio «Corrige main.py» (A04).
    """
    lowered = objective.casefold()
    words = re.findall(r"[^\W_]+", lowered, flags=re.UNICODE)
    has_action = any(word.startswith(_FILE_VERB_PREFIXES) for word in words)
    has_target = any(word.startswith(_FILE_NOUN_PREFIXES) for word in words) or bool(
        _FILE_NAME.search(objective)
    )
    return has_action and has_target


__all__ = [
    "ConnectionReport",
    "DesktopStores",
    "EventCallback",
    "PermissionCallback",
    "RunConfiguration",
    "RunHandle",
    "RunSummary",
    "TaskKind",
    "build_provider",
    "build_registry",
    "check_connection",
    "default_state_dir",
    "list_runs",
    "requires_workspace_change",
    "roll_back_run",
    "run_athena",
    "run_options",
]
