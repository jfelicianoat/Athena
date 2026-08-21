"""Proceso local que expone Athena a clientes externos como ChatyGPT.

El adaptador HTTP y el runtime ya existen en ``athena.adapters.service``. Este
módulo se limita a ensamblarlos con AI_Broker, la persistencia local y una
configuración recibida mediante variables de entorno. No imprime ni persiste
ninguna credencial.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from athena.adapters.ai_broker import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    AiBrokerModelProvider,
)
from athena.adapters.openai_compatible import OpenAICompatibleModelProvider
from athena.adapters.service import AthenaService, RunRegistry, ServiceConfig
from athena.adapters.service.orchestration import OrchestrationSettings
from athena.events import InMemoryEventBus
from athena.graph_store import SqliteGraphStore
from athena.models import ModelProvider
from athena.planning import PlanBoard
from athena.project_memory import SqliteProjectMemory
from athena.provider_router import ProviderEntry, ProviderRegistry, ProviderRouter
from athena.session_store import SqliteSessionStore
from athena.stores import SqliteToolResultStore


def _flag(name: str, *, default: bool) -> bool:
    """Un interruptor del entorno, leído como afirmación o como negación.

    Un valor que no se reconoce es un error, no una de las dos respuestas. `ATHENA_PLANNING=off`
    escrito por alguien que quería apagarlo no puede acabar encendiéndolo por no estar en la
    lista de negaciones.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "y", "on", "si", "sí"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError(f"{name} debe ser sí o no, no {raw!r}")


def _positive(name: str, default: float) -> float:
    """Un número de segundos del entorno, o el de siempre.

    Se valida al arrancar y no al usarse: un plazo mal escrito debe impedir que el
    servicio abra el puerto, no aparecer media hora después dentro de un run.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un número") from exc
    if value <= 0:
        raise ValueError(f"{name} debe ser positivo")
    return value


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    broker_base_url: str
    broker_token: str
    service_token: str
    state_dir: Path
    host: str = "127.0.0.1"
    port: int = 8770
    preferred_model: str | None = None
    #: Un endpoint compatible con OpenAI al que recurrir si el broker deja de poder
    #: atender. Opcional: sin él, un broker caído acaba con el run, que es la conducta
    #: anterior y sigue siendo correcta — sólo menos resistente.
    fallback_base_url: str | None = None
    fallback_model: str | None = None
    #: Cuánto puede tardar una sola llamada HTTP al broker. Con la cola llena, `/health`
    #: tarda ocho segundos y `/api/v1/queue` veinte; treinta segundos convierten un
    #: servidor ocupado en un run fallido.
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    #: Si este despliegue puede descomponer objetivos en un grafo de tareas. Apagado por
    #: defecto: la planificación gasta una llamada al modelo antes de empezar a trabajar,
    #: y quien pone en marcha el servicio es quien sabe si su proveedor da para eso.
    #: Si este despliegue descompone objetivos cuando la evidencia lo justifica. Encendido:
    #: en `auto` la decisión cuesta una lectura del repositorio, no una llamada al modelo,
    #: y sólo se planifica cuando la política ya ha dicho que merece la pena. Se apaga con
    #: `ATHENA_PLANNING=0` para un despliegue que sólo quiera el bucle.
    planning: bool = True
    #: Cuánto se espera a una sola respuesta del modelo antes de dar al broker por
    #: perdido. Medido contra este despliegue: planificar tardó 55 s y un turno de coder
    #: 549 s. Con el techo de diez minutos del adaptador, un run falló 51 segundos
    #: después de que el broker hubiese contestado, que es la peor forma de fallar.
    model_wait_seconds: float = 900.0
    #: Cuánto puede durar una tarea del plan. Tiene que caber más de una llamada al
    #: modelo, o una tarea no llega a terminar ni su primer turno.
    task_timeout_seconds: float = 1800.0

    @classmethod
    def from_environment(cls) -> ServiceSettings:
        broker_url = os.environ.get("ATHENA_BROKER_BASE_URL", "").strip()
        broker_token = os.environ.get("ATHENA_BROKER_TOKEN", "").strip()
        service_token = os.environ.get("ATHENA_SERVICE_TOKEN", "").strip()
        if not broker_url:
            raise ValueError("ATHENA_BROKER_BASE_URL no está configurada")
        if not broker_token:
            raise ValueError("ATHENA_BROKER_TOKEN no está configurado")
        if not service_token:
            raise ValueError("ATHENA_SERVICE_TOKEN no está configurado")

        state_value = os.environ.get("ATHENA_STATE_DIR", "").strip()
        if state_value:
            state_dir = Path(state_value)
        else:
            local = os.environ.get("LOCALAPPDATA", "").strip()
            state_dir = Path(local) / "Athena" / "service" if local else Path.home() / ".athena"

        port_value = os.environ.get("ATHENA_SERVICE_PORT", "8770").strip()
        try:
            port = int(port_value)
        except ValueError as exc:
            raise ValueError("ATHENA_SERVICE_PORT debe ser un número") from exc
        if not 1 <= port <= 65535:
            raise ValueError("ATHENA_SERVICE_PORT está fuera de rango")

        preferred = os.environ.get("ATHENA_PREFERRED_MODEL", "").strip() or None
        fallback_url = os.environ.get("ATHENA_FALLBACK_BASE_URL", "").strip() or None
        fallback_model = os.environ.get("ATHENA_FALLBACK_MODEL", "").strip() or None
        if fallback_url and not fallback_model:
            raise ValueError(
                "ATHENA_FALLBACK_BASE_URL necesita ATHENA_FALLBACK_MODEL: un endpoint "
                "compatible no elige modelo por su cuenta"
            )

        timeout_value = os.environ.get("ATHENA_REQUEST_TIMEOUT_SECONDS", "").strip()
        try:
            timeout = float(timeout_value) if timeout_value else DEFAULT_REQUEST_TIMEOUT_SECONDS
        except ValueError as exc:
            raise ValueError("ATHENA_REQUEST_TIMEOUT_SECONDS debe ser un número") from exc
        if timeout <= 0:
            raise ValueError("ATHENA_REQUEST_TIMEOUT_SECONDS debe ser positivo")

        planning = _flag("ATHENA_PLANNING", default=True)

        model_wait = _positive("ATHENA_MODEL_WAIT_SECONDS", 900.0)
        task_timeout = _positive("ATHENA_TASK_TIMEOUT_SECONDS", 1800.0)
        if task_timeout < model_wait:
            raise ValueError(
                "ATHENA_TASK_TIMEOUT_SECONDS no puede ser menor que "
                "ATHENA_MODEL_WAIT_SECONDS: una tarea que expira antes que la llamada "
                "que la ocupa no termina nunca su primer turno"
            )

        return cls(
            broker_base_url=broker_url,
            broker_token=broker_token,
            service_token=service_token,
            state_dir=state_dir,
            port=port,
            preferred_model=preferred,
            fallback_base_url=fallback_url,
            fallback_model=fallback_model,
            request_timeout_seconds=timeout,
            planning=planning,
            model_wait_seconds=model_wait,
            task_timeout_seconds=task_timeout,
        )


def build_provider(settings: ServiceSettings) -> ModelProvider:
    """AI_Broker, y detrás un endpoint directo si se configuró uno.

    El router es a su vez un `ModelProvider`, así que el AgentLoop sigue hablando con el
    puerto y no se entera de que hay dos. Sólo cae al segundo ante un fallo *permanente*:
    un broker lento reintenta contra el broker, porque cambiar de proveedor por una
    lentitud pasajera cambiaría en silencio qué modelo respondió.
    """
    broker = AiBrokerModelProvider(
        settings.broker_base_url,
        settings.broker_token,
        preferred_model=settings.preferred_model,
        request_timeout_seconds=settings.request_timeout_seconds,
        max_wait_seconds=settings.model_wait_seconds,
    )
    if not settings.fallback_base_url or not settings.fallback_model:
        return broker
    respaldo = OpenAICompatibleModelProvider(
        settings.fallback_base_url,
        settings.fallback_model,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    return ProviderRouter(
        ProviderRegistry(
            ProviderEntry("ai_broker", broker),
            [ProviderEntry("respaldo", respaldo)],
        )
    )


def build_orchestration(settings: ServiceSettings) -> OrchestrationSettings:
    """Lo que este servicio sabe hacer además de ejecutar un bucle.

    Con la planificación apagada no se abre ninguna base de datos de más: un despliegue
    monoagente no debería dejar ficheros de una capa que no usa.

    La memoria de proyecto sí se abre siempre. No depende de que haya grafos —lo que un
    run recuerda de un repositorio le sirve igual sin plan— y es la única de las tres que
    aporta algo a un run que no se descompone.
    """
    memory = SqliteProjectMemory(settings.state_dir / "memory.db")
    if not settings.planning:
        return OrchestrationSettings(memory=memory)
    return OrchestrationSettings(
        planning=True,
        memory=memory,
        graphs=SqliteGraphStore(settings.state_dir / "graphs.db"),
        board=PlanBoard(),
        task_timeout_seconds=settings.task_timeout_seconds,
    )


def build_service(settings: ServiceSettings) -> AthenaService:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    event_bus = InMemoryEventBus()
    provider = build_provider(settings)
    registry = RunRegistry(
        provider,
        event_bus,
        SqliteSessionStore(settings.state_dir / "sessions.db"),
        SqliteToolResultStore(settings.state_dir / "results.db"),
        orchestration=build_orchestration(settings),
    )
    return AthenaService(
        registry,
        ServiceConfig(
            host=settings.host,
            port=settings.port,
            token=settings.service_token,
        ),
    )


async def serve(settings: ServiceSettings) -> None:
    service = build_service(settings)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        # El bucle Proactor de Windows no expone todos los manejadores. El cierre del
        # proceso sigue liberando el socket y SQLite de forma segura.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stopped.set)

    await service.start()
    try:
        await stopped.wait()
    finally:
        await service.stop()


def main() -> int:
    try:
        settings = ServiceSettings.from_environment()
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f"No se pudo iniciar el servicio de Athena: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ServiceSettings",
    "build_orchestration",
    "build_provider",
    "build_service",
    "main",
    "serve",
]
