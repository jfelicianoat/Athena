from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena_service import ServiceSettings, build_orchestration, build_service


def test_service_settings_require_the_three_connection_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ATHENA_BROKER_BASE_URL",
        "ATHENA_BROKER_TOKEN",
        "ATHENA_SERVICE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="ATHENA_BROKER_BASE_URL"):
        ServiceSettings.from_environment()


def test_service_is_assembled_for_chatygpt_without_persisting_tokens(tmp_path: Path) -> None:
    settings = ServiceSettings(
        broker_base_url="http://127.0.0.1:8765",
        broker_token="broker-secret",
        service_token="service-secret",
        state_dir=tmp_path,
        port=8770,
    )

    service = build_service(settings)

    assert service.config.host == "127.0.0.1"
    assert service.config.port == 8770
    assert service.config.token == "service-secret"
    assert (tmp_path / "sessions.db").is_file()
    assert (tmp_path / "results.db").is_file()
    assert "broker-secret" not in repr(service)


def test_service_opens_the_health_endpoint_for_chatygpt(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = build_service(
            ServiceSettings(
                broker_base_url="http://127.0.0.1:9",
                broker_token="test-only",
                service_token="service-test-only",
                state_dir=tmp_path,
                port=0,
            )
        )
        host, port = await service.start()
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()

            assert b"HTTP/1.1 200 OK" in response
            assert b'"wire_version": 1' in response
        finally:
            await service.stop()

    asyncio.run(scenario())


def _settings(state_dir: Path, *, planning: bool) -> ServiceSettings:
    return ServiceSettings(
        broker_base_url="http://127.0.0.1:9",
        broker_token="test-only",
        service_token="service-test-only",
        state_dir=state_dir,
        port=0,
        planning=planning,
    )


def test_a_monoagent_deployment_leaves_no_traces_of_a_layer_it_does_not_use(
    tmp_path: Path,
) -> None:
    """Sin planificación no se abre la base de datos de planes.

    Crearla igualmente sería inofensivo y engañoso: quien mire el directorio de estado
    concluiría que este servicio descompone objetivos, y no lo hace.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    orchestration = build_orchestration(_settings(tmp_path, planning=False))

    assert not orchestration.planning
    assert orchestration.graphs is None
    assert not (tmp_path / "graphs.db").exists()
    # La memoria sí: lo que se recuerda de un repositorio le sirve igual a un run que no
    # se descompone, que son la mayoría.
    assert orchestration.memory is not None
    assert (tmp_path / "memory.db").is_file()


def test_planning_is_switched_on_by_the_deployment_and_persists_its_plans(
    tmp_path: Path,
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    orchestration = build_orchestration(_settings(tmp_path, planning=True))

    assert orchestration.planning
    assert orchestration.graphs is not None
    assert orchestration.board is not None
    assert (tmp_path / "graphs.db").is_file()


def test_planning_stays_off_unless_the_environment_asks_for_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """La variable se lee como una afirmación, no como una presencia.

    `ATHENA_PLANNING=0` es alguien apagándola a propósito. Tratar cualquier valor como
    «sí» convertiría ese apagado explícito en lo contrario de lo que dice.
    """
    monkeypatch.setenv("ATHENA_BROKER_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATHENA_BROKER_TOKEN", "test-only")
    monkeypatch.setenv("ATHENA_SERVICE_TOKEN", "service-test-only")
    monkeypatch.setenv("ATHENA_STATE_DIR", str(tmp_path))

    monkeypatch.delenv("ATHENA_PLANNING", raising=False)
    assert not ServiceSettings.from_environment().planning

    monkeypatch.setenv("ATHENA_PLANNING", "0")
    assert not ServiceSettings.from_environment().planning

    monkeypatch.setenv("ATHENA_PLANNING", "true")
    assert ServiceSettings.from_environment().planning


def test_a_task_may_not_expire_before_the_call_it_is_waiting_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Una tarea con menos reloj que su llamada al modelo no termina ni su primer turno.

    Se comprueba al arrancar, no al usarse: un plazo mal puesto tiene que impedir que el
    servicio abra el puerto, en vez de aparecer media hora después dentro de un run.
    """
    monkeypatch.setenv("ATHENA_BROKER_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATHENA_BROKER_TOKEN", "test-only")
    monkeypatch.setenv("ATHENA_SERVICE_TOKEN", "service-test-only")
    monkeypatch.setenv("ATHENA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ATHENA_MODEL_WAIT_SECONDS", "900")
    monkeypatch.setenv("ATHENA_TASK_TIMEOUT_SECONDS", "300")

    with pytest.raises(ValueError, match="ATHENA_TASK_TIMEOUT_SECONDS"):
        ServiceSettings.from_environment()


def test_the_deployment_sets_how_slow_slow_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Los plazos por defecto son de este despliegue, no del adaptador.

    El techo de diez minutos del adaptador hizo fallar un run cincuenta y un segundos
    después de que el broker hubiese contestado. Contra un modelo local de 30B, nueve
    minutos por turno no es un broker roto: es el martes.
    """
    monkeypatch.setenv("ATHENA_BROKER_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATHENA_BROKER_TOKEN", "test-only")
    monkeypatch.setenv("ATHENA_SERVICE_TOKEN", "service-test-only")
    monkeypatch.setenv("ATHENA_STATE_DIR", str(tmp_path))
    for name in ("ATHENA_MODEL_WAIT_SECONDS", "ATHENA_TASK_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    settings = ServiceSettings.from_environment()
    assert settings.model_wait_seconds == 900.0
    assert settings.task_timeout_seconds == 1800.0

    monkeypatch.setenv("ATHENA_MODEL_WAIT_SECONDS", "60")
    assert ServiceSettings.from_environment().model_wait_seconds == 60.0

    monkeypatch.setenv("ATHENA_MODEL_WAIT_SECONDS", "cuando toque")
    with pytest.raises(ValueError, match="ATHENA_MODEL_WAIT_SECONDS"):
        ServiceSettings.from_environment()


def test_planning_hands_its_clock_to_the_tasks(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    orchestration = build_orchestration(_settings(tmp_path, planning=True))

    assert orchestration.task_timeout_seconds == 1800.0
