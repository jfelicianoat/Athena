from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _no_attach_grace() -> Iterator[None]:
    """Sin ventana de enganche salvo en las pruebas que la estudian.

    En produccion un run espera unos segundos a que se suscriba el cliente que lo creo
    antes de denegar una pregunta por «no hay nadie». Las pruebas que no van de eso
    arrancan runs sin cliente a proposito, y cada una pagaria esa espera.
    """
    from athena.adapters.service import approvals

    original = approvals.DEFAULT_ATTACH_GRACE_SECONDS
    approvals.DEFAULT_ATTACH_GRACE_SECONDS = 0.0
    try:
        yield
    finally:
        approvals.DEFAULT_ATTACH_GRACE_SECONDS = original


@pytest.fixture(autouse=True)
def _sin_conexion_real_con_core(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core deja URL y token de cada app en %LOCALAPPDATA%/AIOSCore/conexion: los tests no la ven."""
    monkeypatch.setenv("AI_OS_CONNECT_DIR", str(tmp_path_factory.mktemp("conexion_vacia")))
