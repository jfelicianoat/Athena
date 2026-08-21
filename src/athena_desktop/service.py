"""Managed Athena service process used by the desktop application."""

from __future__ import annotations

import http.client
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from athena.adapters.service.launch import ServiceEndpoint, parse_service_ready


class ServiceAlreadyRunning(RuntimeError):
    """A healthy external Athena owns the requested address."""

    def __init__(self, base_url: str) -> None:
        super().__init__(f"Athena ya está disponible en {base_url}")
        self.base_url = base_url


@dataclass(frozen=True, slots=True)
class ManagedServiceRequest:
    broker_base_url: str
    broker_token: str
    state_dir: Path
    preferred_model: str = ""
    port: int = 8770


@dataclass(slots=True)
class ManagedAthenaService:
    endpoint: ServiceEndpoint
    process: subprocess.Popen[str]

    def stop(self, timeout_seconds: float = 5.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout_seconds)


def default_service_state_dir(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    local = env.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "Athena" / "service"
    return Path.home() / ".athena" / "service"


def start_managed_service(
    request: ManagedServiceRequest,
    *,
    timeout_seconds: float = 15.0,
) -> ManagedAthenaService:
    if not request.broker_base_url.strip():
        raise ValueError("La URL de AI_Broker es obligatoria")
    if not request.broker_token.strip():
        raise ValueError("El token de AI_Broker es obligatorio")
    if not 0 <= request.port <= 65535:
        raise ValueError("El puerto del servicio de Athena está fuera de rango")
    if request.port:
        requested_url = f"http://127.0.0.1:{request.port}"
        if _is_athena_service("127.0.0.1", request.port):
            raise ServiceAlreadyRunning(requested_url)

    environment = dict(os.environ)
    environment.update(
        {
            "ATHENA_BROKER_BASE_URL": request.broker_base_url.strip(),
            "ATHENA_BROKER_TOKEN": request.broker_token.strip(),
            "ATHENA_STATE_DIR": str(request.state_dir),
            "ATHENA_SERVICE_PORT": str(request.port),
        }
    )
    source_root = str(Path(__file__).resolve().parents[1])
    inherited_pythonpath = environment.get("PYTHONPATH", "").strip()
    environment["PYTHONPATH"] = (
        source_root + os.pathsep + inherited_pythonpath if inherited_pythonpath else source_root
    )
    # A managed start always mints a fresh credential. The child announces it only through
    # its captured stdout, so it is neither persisted nor inherited accidentally.
    environment.pop("ATHENA_SERVICE_TOKEN", None)
    if request.preferred_model.strip():
        environment["ATHENA_PREFERRED_MODEL"] = request.preferred_model.strip()
    else:
        environment.pop("ATHENA_PREFERRED_MODEL", None)

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "athena_service"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
        creationflags=creationflags,
    )
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()

    def read_lines() -> None:
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=read_lines, name="athena-service-output", daemon=True).start()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = _stderr(process)
            raise RuntimeError(detail or "El servicio de Athena terminó durante el arranque")
        try:
            line = lines.get(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            continue
        endpoint = parse_service_ready(line.strip())
        if endpoint is not None:
            return ManagedAthenaService(endpoint, process)

    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
    raise TimeoutError("Athena no anunció su token antes de agotar el tiempo de arranque")


def _stderr(process: subprocess.Popen[str]) -> str:
    if process.stderr is None:
        return ""
    return process.stderr.read().strip()


def _is_athena_service(host: str, port: int) -> bool:
    connection = http.client.HTTPConnection(host, port, timeout=0.75)
    try:
        connection.request("GET", "/v1/health")
        response = connection.getresponse()
        content = response.read()
    except (OSError, TimeoutError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    if response.status != 200:
        return False
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and isinstance(payload.get("wire_version"), int)
    )


__all__ = [
    "ManagedAthenaService",
    "ManagedServiceRequest",
    "ServiceAlreadyRunning",
    "default_service_state_dir",
    "start_managed_service",
]
