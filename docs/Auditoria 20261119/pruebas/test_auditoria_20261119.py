"""Pruebas de la auditoría del 19-nov-2026 sobre Athena 0.3.7.

Cada prueba afirma el comportamiento ESPERADO (y de dónde sale esa expectativa). Una
prueba que falla es un defecto abierto; al corregirlo debe pasar sin tocar la prueba.

    .venv/Scripts/python.exe -m pytest "docs/Auditoria 20261119/pruebas" -q -p no:cacheprovider

Aisladas: AI_OS_CONNECT_DIR y LOCALAPPDATA apuntan a carpetas temporales; los dobles HTTP
de `dobles.py` hacen de Broker y de Core.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dobles import FakeBroker, FakeCore

from athena.adapters.ai_os_core import (
    AiOsCoreGovernor,
    CoreUnavailableError,
    governor_from_environment,
)
from athena_desktop.config import DEFAULT_URLS, ProviderKind
from athena_desktop.runtime import (
    RunConfiguration,
    check_connection,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _aislado(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "conexion_vacia"
    empty.mkdir()
    monkeypatch.setenv("AI_OS_CONNECT_DIR", str(empty))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for name in ("ATHENA_CORE_URL", "ATHENA_CORE_TOKEN", "ATHENA_BROKER_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _connect_to(core: FakeCore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "conexion"
    folder.mkdir()
    (folder / "athena.json").write_text(
        json.dumps({"core_url": core.url, "token": "tok-core"}), encoding="utf-8"
    )
    monkeypatch.setenv("AI_OS_CONNECT_DIR", str(folder))


# --------------------------------------------------------------- H-01 Probar conexión


def test_h01_probar_conexion_acepta_el_token_que_da_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Esperado: la propia ventana dice «Con AI OS Core no hace falta [el token]» y
    `RunConfiguration.validate()` acepta el campo vacío con Core. «Probar conexión»
    debería usar la misma regla y conectar."""
    broker = FakeBroker()
    core = FakeCore("allow", broker_token=broker.token)
    try:
        _connect_to(core, tmp_path, monkeypatch)
        configuration = RunConfiguration(
            workspace=tmp_path,
            objective="probar",
            provider=ProviderKind.AI_BROKER,
            base_url=broker.url,
            model="modelo-bueno",
            token="",
        )
        configuration.validate()  # Iniciar sí lo acepta
        report = asyncio.run(check_connection(configuration))
        assert report.ok, report.message
    finally:
        broker.close()
        core.close()


# --------------------------------------------------------------- H-03 Core no-JSON


def test_h03_core_que_contesta_algo_que_no_es_json_se_trata_como_no_disponible() -> None:
    """Esperado (docstring del adaptador y README): si Core no da un ALLOW ejecutable, el run
    no se crea y el servicio responde 503 `core_unavailable` o 403 `core_denied`; nunca un
    error interno. Un proxy que contesta HTML 200 es «Core no disponible»."""
    core = FakeCore("html")
    try:
        governor = AiOsCoreGovernor(core.url, "t")
        with pytest.raises(CoreUnavailableError):
            asyncio.run(governor.authorize("run-1", {}))
    finally:
        core.close()


def test_h03_core_que_contesta_una_lista_se_trata_como_no_disponible() -> None:
    core = FakeCore("lista")
    try:
        governor = AiOsCoreGovernor(core.url, "t")
        with pytest.raises(CoreUnavailableError):
            asyncio.run(governor.authorize("run-1", {}))
    finally:
        core.close()


def test_h03_un_recibo_con_respuesta_no_json_no_lanza() -> None:
    """Esperado (docstring de `receipt`): «Un recibo perdido no tumba el run»."""
    core = FakeCore("html")
    try:
        governor = AiOsCoreGovernor(core.url, "t")
        # policies/evaluate da HTML; los recibos los contesta con JSON. Forzamos HTML
        # en el recibo apuntando a la ruta de evaluación.
        governor._base_url = core.url
        original = governor._post

        def post_html(path: str, payload: dict[str, object], timeout: float | None = None):
            return original("/api/v1/policies/evaluate", payload, timeout)

        governor._post = post_html  # type: ignore[method-assign]
        asyncio.run(governor.receipt("run-1", "succeeded", {}))
    finally:
        core.close()


# --------------------------------------------------------------- H-04 Core implícito


def test_h04_la_documentacion_dice_que_el_fichero_de_conexion_activa_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La conducta es deliberada (docstring de `core_connection`: Core deja el token en su
    carpeta de conexión «así no hay que pegar ninguno»). El defecto es documental: README y
    docstring del módulo dicen que sin `ATHENA_CORE_URL` nada cambia."""
    core = FakeCore("allow")
    try:
        _connect_to(core, tmp_path, monkeypatch)
        assert governor_from_environment() is not None  # conducta decidida: no cambia
    finally:
        core.close()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("## AI OS Core as director") :]
    section = section[: section.index("\n## ", 5)]
    assert "conexion" in section, "el README no dice que el fichero de conexión activa Core"
    module = (ROOT / "src" / "athena" / "adapters" / "ai_os_core.py").read_text(encoding="utf-8")
    assert "sin `ATHENA_CORE_URL` todo sigue igual" not in module


# --------------------------------------------------------------- H-08 URL por defecto


def test_h08_url_por_defecto_del_broker_es_la_del_ecosistema() -> None:
    """Esperado: decisión del dueño del 20-oct-2026 (Broker solo HTTPS en 192.168.1.52:8765;
    Model_Drift 0.4.4, Local AI Lab 0.1.6, KO 0.4.8, ChatyGPT 0.4.11 y Trading 1.3.4 pasaron
    a ese valor por defecto). No está escrita en el repo de Athena: confirmarla con el dueño.
    En Athena una instalación nueva propone http://localhost:8000."""
    assert DEFAULT_URLS["ai_broker"] == "https://192.168.1.52:8765"


# H-05 no tiene prueba unitaria: su regresión es `h05_reanudar.py`, que maneja la ventana
# real y sale con código 1 si el titular contradice los ficheros cambiados.
