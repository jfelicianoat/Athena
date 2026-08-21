from __future__ import annotations

import http.client
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from athena.adapters.service.launch import ServiceEndpoint, parse_service_ready
from athena_desktop.service import (
    ManagedServiceRequest,
    ServiceAlreadyRunning,
    ServiceState,
    start_managed_service,
)


def test_desktop_starts_athena_and_captures_its_generated_token(tmp_path: Path) -> None:
    managed = start_managed_service(
        ManagedServiceRequest(
            broker_base_url="http://127.0.0.1:9",
            broker_token="broker-test-only",
            state_dir=tmp_path,
            port=0,
        ),
        timeout_seconds=10,
    )
    try:
        assert managed.endpoint.token
        connection = http.client.HTTPConnection(
            managed.endpoint.host,
            managed.endpoint.port,
            timeout=2,
        )
        connection.request("GET", "/v1/health")
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 200
    finally:
        managed.stop()


def test_desktop_attaches_first_and_does_not_start_a_second_athena(tmp_path: Path) -> None:
    first = start_managed_service(
        ManagedServiceRequest(
            broker_base_url="http://127.0.0.1:9",
            broker_token="broker-test-only",
            state_dir=tmp_path / "first",
            port=0,
        ),
        timeout_seconds=10,
    )
    try:
        with pytest.raises(ServiceAlreadyRunning) as caught:
            start_managed_service(
                ManagedServiceRequest(
                    broker_base_url="http://127.0.0.1:9",
                    broker_token="broker-test-only",
                    state_dir=tmp_path / "second",
                    port=first.endpoint.port,
                ),
                timeout_seconds=10,
            )

        assert caught.value.base_url == first.endpoint.base_url
        assert first.process.poll() is None
    finally:
        first.stop()


def test_the_error_channel_has_a_reader_while_the_service_runs(tmp_path: Path) -> None:
    """La tubería de errores se vacía mientras el servicio vive, no sólo al morir.

    El buffer del sistema son decenas de kilobytes y `logging` manda ahí los avisos por
    defecto: un servicio levantado horas avisando de un broker inestable acabaría parado
    escribiendo en una tubería que nadie lee, sin log y sin causa aparente. Antes sólo se
    leía en el camino de fallo, que es exactamente cuando ya es tarde.

    Se comprueba que el hilo existe y no que la tubería no se llena: llenarla de verdad
    exigiría un hijo que escupa cien kilobytes a discreción, y este hijo es el servicio.
    """
    managed = start_managed_service(
        ManagedServiceRequest(
            broker_base_url="http://127.0.0.1:9",
            broker_token="broker-test-only",
            state_dir=tmp_path,
            port=0,
        ),
        timeout_seconds=10,
    )
    try:
        lectores = {hilo.name for hilo in threading.enumerate() if hilo.is_alive()}
        assert "athena-service-errors" in lectores, "nadie está vaciando el canal de errores"
        assert "athena-service-output" in lectores
        assert managed.process.poll() is None
    finally:
        managed.stop()


def test_a_stop_its_owner_asked_for_is_not_a_failure(tmp_path: Path) -> None:
    """Windows devuelve 1 al terminar un proceso, y eso no es una caída.

    Sin registrar que la parada se pidió, un cierre limpio y un cuelgue se ven igual desde
    fuera, y a una persona se le contaría el primero como el segundo.
    """
    managed = start_managed_service(
        ManagedServiceRequest(
            broker_base_url="http://127.0.0.1:9",
            broker_token="broker-test-only",
            state_dir=tmp_path,
            port=0,
        ),
        timeout_seconds=10,
    )
    # Se toman las dos lecturas por separado: `state` cambia con el proceso, y afirmar
    # sobre ella dos veces seguidas la hace parecer estable a quien lee —y a mypy, que
    # estrecha el tipo tras la primera y da la segunda por imposible.
    antes = managed.state
    managed.stop()
    despues = managed.state

    assert antes is ServiceState.RUNNING
    assert despues is ServiceState.STOPPED
    # El código de salida sigue siendo el que da el sistema; lo que cambia es qué se
    # concluye de él.
    assert managed.process.poll() is not None


def test_a_service_that_dies_on_its_own_is_a_failure(tmp_path: Path) -> None:
    """Y el otro lado de lo mismo: nadie lo paró, así que se cayó."""
    managed = start_managed_service(
        ManagedServiceRequest(
            broker_base_url="http://127.0.0.1:9",
            broker_token="broker-test-only",
            state_dir=tmp_path,
            port=0,
        ),
        timeout_seconds=10,
    )
    try:
        managed.process.kill()
        managed.process.wait(timeout=5)
        assert managed.state is ServiceState.FAILED
    finally:
        managed.stop()


def test_the_credential_is_announced_once_and_never_written_again(tmp_path: Path) -> None:
    """El handshake dice el token una vez; nada más vuelve a decirlo.

    Athena no lo persiste, pero sí lo anuncia por stdout, y quien redirige esa salida a un
    fichero lo guarda —comprobado aquí de la forma más literal posible, redirigiendo—. Que
    aparezca *una* vez y sólo una es lo que hace que redirigir tenga una consecuencia
    acotada en vez de sembrar la credencial por todo un registro.

    Se lanza el proceso a mano en vez de por `start_managed_service` porque ese camino
    tiene hilos vaciando ambas tuberías, y leerlas desde aquí competiría con ellos.
    """
    salida = tmp_path / "stdout.log"
    errores = tmp_path / "stderr.log"
    environment = dict(os.environ)
    environment.update(
        {
            "ATHENA_BROKER_BASE_URL": "http://127.0.0.1:9",
            "ATHENA_BROKER_TOKEN": "broker-test-only",
            "ATHENA_STATE_DIR": str(tmp_path / "state"),
            "ATHENA_SERVICE_PORT": "0",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
    )
    environment.pop("ATHENA_SERVICE_TOKEN", None)

    with salida.open("w", encoding="utf-8") as out, errores.open("w", encoding="utf-8") as err:
        process = subprocess.Popen(
            [sys.executable, "-m", "athena_service"],
            stdout=out,
            stderr=err,
            env=environment,
            text=True,
        )
        try:
            endpoint = _await_ready(salida, process)
            connection = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=5)
            # Tráfico que atraviesa el registro. Una petición sin credencial y otra con
            # una equivocada son las que tentarían a un log a repetir el token para
            # explicar por qué no coincide.
            for headers in ({}, {"Authorization": "Bearer credencial-equivocada"}):
                connection.request("GET", "/v1/runs", headers=headers)
                connection.getresponse().read()
            connection.close()
        finally:
            process.terminate()
            process.wait(timeout=10)

    registrado = salida.read_text(encoding="utf-8") + errores.read_text(encoding="utf-8")
    assert registrado.count(endpoint.token) == 1, "la credencial se dijo más de una vez"


def _await_ready(salida: Path, process: subprocess.Popen[str]) -> ServiceEndpoint:
    """Espera la línea del handshake leyendo el fichero al que se redirigió."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("el servicio terminó antes de anunciarse")
        for line in salida.read_text(encoding="utf-8").splitlines():
            endpoint = parse_service_ready(line.strip())
            if endpoint is not None:
                return endpoint
        time.sleep(0.1)
    raise AssertionError("el servicio no se anunció a tiempo")
