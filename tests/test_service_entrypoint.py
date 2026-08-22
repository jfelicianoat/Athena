from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from athena.adapters.service.launch import parse_service_ready
from athena_service import ServiceSettings, build_orchestration, build_service, serve


def test_service_settings_require_broker_credentials_and_generate_the_service_token(
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

    monkeypatch.setenv("ATHENA_BROKER_BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ATHENA_BROKER_TOKEN", "broker-secret")

    first = ServiceSettings.from_environment()
    second = ServiceSettings.from_environment()

    assert first.service_token
    assert second.service_token
    assert first.service_token != second.service_token


def test_service_announces_the_generated_token_after_opening_the_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        settings = ServiceSettings(
            broker_base_url="http://127.0.0.1:9",
            broker_token="test-only",
            service_token="generated-service-token",
            state_dir=tmp_path,
            port=0,
        )
        ready = asyncio.Event()
        announced: list[str] = []

        async def stop_after_announcement() -> None:
            while not announced:
                await asyncio.sleep(0)
            ready.set()

        waiter = asyncio.create_task(stop_after_announcement())
        task = asyncio.create_task(
            serve(settings, announce=lambda line: announced.append(line), stop=ready)
        )
        await asyncio.wait_for(waiter, timeout=2)
        await asyncio.wait_for(task, timeout=2)

        endpoint = parse_service_ready(announced[0])
        assert endpoint is not None
        assert endpoint.token == "generated-service-token"
        assert endpoint.base_url.startswith("http://127.0.0.1:")

    asyncio.run(scenario())


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


def test_deciding_is_the_normal_behaviour_and_apagarlo_es_lo_explicito(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`auto` es como corre Athena normalmente, así que no hace falta pedirlo.

    Decidir cuesta una lectura del repositorio, no una llamada al modelo: sólo se
    planifica cuando la política ya ha dicho que merece la pena. Lo que hay que declarar
    es lo contrario — un despliegue que sólo quiera el bucle.

    Un valor que no se reconoce es un error y no una de las dos respuestas: quien escribe
    `off` queriendo apagarlo no puede acabar encendiéndolo por no estar en la lista.
    """
    monkeypatch.setenv("ATHENA_BROKER_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATHENA_BROKER_TOKEN", "test-only")
    monkeypatch.setenv("ATHENA_SERVICE_TOKEN", "service-test-only")
    monkeypatch.setenv("ATHENA_STATE_DIR", str(tmp_path))

    monkeypatch.delenv("ATHENA_PLANNING", raising=False)
    assert ServiceSettings.from_environment().planning

    for apagado in ("0", "false", "no", "off"):
        monkeypatch.setenv("ATHENA_PLANNING", apagado)
        assert not ServiceSettings.from_environment().planning, apagado

    monkeypatch.setenv("ATHENA_PLANNING", "true")
    assert ServiceSettings.from_environment().planning

    monkeypatch.setenv("ATHENA_PLANNING", "cuando haga falta")
    with pytest.raises(ValueError, match="ATHENA_PLANNING"):
        ServiceSettings.from_environment()


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


def test_liveness_and_authentication_are_different_questions(tmp_path: Path) -> None:
    """`/v1/health` dice que Athena vive; `/v1/auth/check` dice si te conoce.

    Hacen falta las dos por separado. `/v1/health` es público a propósito —un sondeo de
    vida que exigiese credencial no sirve para saber si hay que arrancar el servicio— y
    por eso no puede usarse para concluir que un cliente está autenticado. Cuando se usaba
    para eso, la aplicación se anunciaba conectada mientras cada operación devolvía 401.
    """

    async def scenario() -> None:
        service = build_service(
            ServiceSettings(
                broker_base_url="http://127.0.0.1:9",
                broker_token="test-only",
                service_token="la-buena",
                state_dir=tmp_path,
                port=0,
            )
        )
        host, port = await service.start()
        try:

            async def pedir(ruta: str, credencial: str | None) -> int:
                reader, writer = await asyncio.open_connection(host, port)
                cabeceras = f"GET {ruta} HTTP/1.1\r\nHost: localhost\r\n"
                if credencial is not None:
                    cabeceras += f"Authorization: Bearer {credencial}\r\n"
                writer.write((cabeceras + "\r\n").encode())
                await writer.drain()
                crudo = await reader.read()
                writer.close()
                await writer.wait_closed()
                return int(crudo.split(b"\r\n")[0].split(b" ")[1])

            # Vivo, y lo dice sin que nadie se identifique.
            assert await pedir("/v1/health", None) == 200
            # La misma instancia, preguntada por la credencial, distingue los tres casos.
            assert await pedir("/v1/auth/check", None) == 401
            assert await pedir("/v1/auth/check", "la-mala") == 401
            assert await pedir("/v1/auth/check", "la-buena") == 200
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_the_check_answers_the_one_question_and_carries_nothing_else(tmp_path: Path) -> None:
    """Sin datos del servicio: un endpoint barato al que se puede llamar a menudo.

    Devolver runs, versiones o rutas lo convertiría en algo que se sondea por sus datos, y
    un cliente acabaría dependiendo de que una comprobación de credencial le informe.
    """

    async def scenario() -> None:
        service = build_service(
            ServiceSettings(
                broker_base_url="http://127.0.0.1:9",
                broker_token="test-only",
                service_token="la-buena",
                state_dir=tmp_path,
                port=0,
            )
        )
        host, port = await service.start()
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                b"GET /v1/auth/check HTTP/1.1\r\nHost: localhost\r\n"
                b"Authorization: Bearer la-buena\r\n\r\n"
            )
            await writer.drain()
            crudo = await reader.read()
            writer.close()
            await writer.wait_closed()
        finally:
            await service.stop()

        cuerpo = json.loads(crudo.split(b"\r\n\r\n", 1)[1])
        assert cuerpo == {"authenticated": True, "wire_version": 1}
        # Y la credencial no viaja de vuelta, ni siquiera para confirmarla.
        assert b"la-buena" not in crudo.split(b"\r\n\r\n", 1)[1]

    asyncio.run(scenario())


def test_the_service_reports_what_it_has_measured(tmp_path: Path) -> None:
    """La medición sale por HTTP, que es como llega a quien tiene que leerla.

    Agregada y comparada por estrategia: quien pregunta quiere saber si descomponer sale a
    cuenta. Devolver la lista de runs haría que la respuesta creciera con el uso hasta ser
    inservible por su propio tamaño.

    Sin runs todavía la respuesta son ceros y no un error: «aún no hay datos» es una
    respuesta legítima para un panel, y una excepción no lo es.
    """

    async def scenario() -> None:
        service = build_service(
            ServiceSettings(
                broker_base_url="http://127.0.0.1:9",
                broker_token="test-only",
                service_token="la-buena",
                state_dir=tmp_path,
                port=0,
            )
        )
        host, port = await service.start()
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                b"GET /v1/metrics HTTP/1.1\r\nHost: localhost\r\n"
                b"Authorization: Bearer la-buena\r\n\r\n"
            )
            await writer.drain()
            crudo = await reader.read()
            writer.close()
            await writer.wait_closed()
        finally:
            await service.stop()

        assert int(crudo.split(b"\r\n")[0].split(b" ")[1]) == 200
        cuerpo = json.loads(crudo.split(b"\r\n\r\n", 1)[1])
        assert isinstance(cuerpo, dict)

    asyncio.run(scenario())


def test_metrics_need_the_credential_like_everything_else(tmp_path: Path) -> None:
    """No es un sondeo de vida: lo medido es información del trabajo de alguien."""

    async def scenario() -> None:
        service = build_service(
            ServiceSettings(
                broker_base_url="http://127.0.0.1:9",
                broker_token="test-only",
                service_token="la-buena",
                state_dir=tmp_path,
                port=0,
            )
        )
        host, port = await service.start()
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(b"GET /v1/metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            crudo = await reader.read()
            writer.close()
            await writer.wait_closed()
        finally:
            await service.stop()

        assert int(crudo.split(b"\r\n")[0].split(b" ")[1]) == 401

    asyncio.run(scenario())
