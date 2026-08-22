"""Arrancar Telegram: la parte que faltaba para que el canal existiera de verdad.

El paquete traía el cableado escrito en su docstring y ningún punto de entrada, así que
usarlo exigía copiar el fragmento en un script propio. Un subsistema completo, probado, y
que no arrancaba nadie.

Lo que se prueba aquí no es que Telegram funcione —de eso se ocupa
`test_telegram_adapter.py`— sino que **negarse a arrancar** es lo que hace ante una
configuración que sería peligrosa.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.adapters.service.runs import CapabilityMode
from athena_service import ServiceSettings
from athena_telegram.__main__ import (
    DEFAULT_GRANT_OPTIONS,
    parse_workspaces,
    serve,
)
from athena_telegram.config import TelegramConfigError

# -- la configuración --------------------------------------------------------


def test_una_ruta_de_windows_no_se_parte_por_su_propio_dos_puntos() -> None:
    """`D:/repo` lleva dos puntos, y partir por el último dejaría a la persona sin carpeta."""
    grants = parse_workspaces("12345:D:/Desarrollo/repo")

    assert grants["telegram:12345"].workspace_root == Path("D:/Desarrollo/repo")


def test_cada_identidad_tiene_su_carpeta() -> None:
    """Una raíz compartida convertiría el permiso de hablar con el bot en permiso sobre
    todo lo que el bot alcanza."""
    grants = parse_workspaces("1:D:/uno;2:D:/dos")

    assert grants["telegram:1"].workspace_root != grants["telegram:2"].workspace_root


def test_sin_carpetas_no_arranca() -> None:
    """No hay valor por defecto razonable para «sobre qué trabaja un desconocido»."""
    for vacio in (None, "", "   ", ";;"):
        with pytest.raises(TelegramConfigError):
            parse_workspaces(vacio)


def test_una_entrada_mal_escrita_es_un_error_y_no_se_ignora() -> None:
    """Saltársela dejaría a alguien sin acceso creyendo que lo tiene."""
    with pytest.raises(TelegramConfigError):
        parse_workspaces("12345")
    with pytest.raises(TelegramConfigError):
        parse_workspaces("12345:")


# -- lo que un chat puede pedir ---------------------------------------------


def test_un_chat_escribe_pero_no_ejecuta() -> None:
    """La asimetría es deliberada.

    Una escritura queda en el workspace y se puede leer, revertir y discutir; un comando
    puede hacer cualquier cosa que pueda hacer la máquina. Concederlo por comodidad sería
    dar la capacidad más peligrosa por el canal con la identidad más débil.
    """
    assert DEFAULT_GRANT_OPTIONS.writes is CapabilityMode.ALLOW
    assert DEFAULT_GRANT_OPTIONS.execution is CapabilityMode.OFF


# -- negarse a arrancar ------------------------------------------------------


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        broker_base_url="http://127.0.0.1:1",
        broker_token="t",
        service_token="s",
        state_dir=tmp_path / "estado",
        host="127.0.0.1",
        port=0,
    )


def test_sin_lista_de_permitidos_no_arranca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un bot sin lista es un agente atendiendo a cualquiera que le escriba.

    Y arranca en el mismo proceso que el servicio, así que un fallo abierto aquí no sería
    sólo un bot mal configurado: sería el runtime entero al alcance de un desconocido.
    """
    monkeypatch.setenv("ATHENA_TELEGRAM_TOKEN", "123:abc")
    monkeypatch.delenv("ATHENA_TELEGRAM_ALLOWED_IDS", raising=False)
    monkeypatch.setenv("ATHENA_TELEGRAM_WORKSPACES", f"1:{tmp_path}")

    with pytest.raises(TelegramConfigError, match="ALLOWED_IDS"):
        asyncio.run(serve(_settings(tmp_path)))


def test_una_carpeta_dada_a_quien_nadie_escucha_es_un_descuido(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Las dos listas existen para fallar por separado, pero esto no es defensa en
    profundidad: quien lo escribió creía estar dando acceso y no lo dio."""
    monkeypatch.setenv("ATHENA_TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("ATHENA_TELEGRAM_ALLOWED_IDS", "111")
    monkeypatch.setenv("ATHENA_TELEGRAM_WORKSPACES", f"222:{tmp_path}")

    with pytest.raises(TelegramConfigError, match="telegram:222"):
        asyncio.run(serve(_settings(tmp_path)))


def test_sin_token_no_arranca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATHENA_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("ATHENA_TELEGRAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("ATHENA_TELEGRAM_ALLOWED_IDS", "111")
    monkeypatch.setenv("ATHENA_TELEGRAM_WORKSPACES", f"111:{tmp_path}")

    with pytest.raises(TelegramConfigError):
        asyncio.run(serve(_settings(tmp_path)))


def test_el_canal_no_monta_un_runtime_aparte() -> None:
    """Dos procesos serían dos registros, y un run de Telegram no existiría para ChatyGPT.

    `identity.py` existe para que una persona en los dos sitios sea la misma persona;
    darle dos runtimes separados lo desmentiría desde el primer minuto. Esta prueba mira
    el código porque la propiedad es estructural: el arranque usa el registro del servicio
    que acaba de construir, no uno suyo.
    """
    import athena_telegram.__main__ as arranque

    fuente = Path(arranque.__file__).read_text(encoding="utf-8")

    assert "service.registry" in fuente
    assert "RunRegistry(" not in fuente, "el canal se estaba montando su propio registro"
