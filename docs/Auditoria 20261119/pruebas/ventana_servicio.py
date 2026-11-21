"""Pestaña «Servicio para otras apps»: Iniciar → Copiar token → health/auth → Detener.

Ventana real en proceso (arnes_ventana.py), Broker falso, LOCALAPPDATA y conexión de Core
aislados. Usa el puerto 8770 del servicio gestionado: comprobar antes que está libre.

Uso: python ventana_servicio.py <carpeta_salida>
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arnes_ventana as a  # noqa: E402
from dobles import FakeBroker  # noqa: E402


def ask(port: int, path: str, token: str = "") -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
    status = conn.getresponse().status
    conn.close()
    return status


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="athena-svc-"))
    broker = FakeBroker()
    a.isolate(base)
    a.patch_messagebox()
    root, app = a.make_app(base)
    r = a.Runner(root, app, out)
    results: dict[str, Any] = {}

    def start() -> None:
        app.base_url.set(broker.url)
        app._switch_credential()
        app.token.set(broker.token)
        app.tabs.select(2)
        app._start_service()

    r.step("iniciar servicio", start, lambda: bool(app.service_token.get()), 60)

    def copy() -> None:
        app._copy_service_token()
        clip = root.clipboard_get()
        url = app.service_url.get()
        port = int(url.rsplit(":", 1)[1]) if url else 8770
        results["S1"] = {
            "captura": r.shot("servicio_en_marcha"),
            "estado": app.service_status.get(),
            "url": url,
            "portapapeles_es_el_token": clip == app.service_token.get() and bool(clip),
            "health": ask(port, "/v1/health"),
            "auth_con_token": ask(port, "/v1/auth/check", clip),
            "auth_sin_token": ask(port, "/v1/auth/check"),
        }

    r.step("copiar token y comprobarlo", copy)
    r.step("detener", lambda: app._stop_service(), lambda: not app.service_token.get(), 30)

    def after() -> None:
        port = int(results["S1"]["url"].rsplit(":", 1)[1]) if results["S1"]["url"] else 8770
        try:
            gone = ask(port, "/v1/health")
        except OSError:
            gone = "sin respuesta (puerto cerrado)"
        results["S2"] = {
            "captura": r.shot("servicio_detenido"),
            "estado": app.service_status.get(),
            "health_tras_detener": gone,
        }

    r.step("tras detener", after)
    r.run()
    results["dialogos"] = a.DIALOGS
    results["pasos"] = r.log
    (out / "ventana_servicio.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    broker.close()
    print(
        json.dumps(
            {k: v for k, v in results.items() if k.startswith("S")}, indent=2, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
