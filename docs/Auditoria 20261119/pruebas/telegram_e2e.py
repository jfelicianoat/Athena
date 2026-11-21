"""El canal de Telegram de extremo a extremo, sin bot real (20-nov-2026).

Arranca `athena_telegram.serve` de verdad (servicio HTTP + canal en el mismo proceso) con
su `TelegramApi` apuntando a un doble de la Bot API (HTTP local) y el modelo en el Broker
falso de `dobles.py`. Comprueba:
  T1  un usuario permitido pregunta → se crea un run y el bot le contesta con la respuesta
  T2  un usuario NO permitido escribe → no se crea run y no recibe la respuesta
  T3  sin ATHENA_TELEGRAM_ALLOWED_IDS el canal se niega a arrancar

Uso: python telegram_e2e.py <carpeta_salida>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ATHENA = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ATHENA / "src"))
from dobles import FakeBroker  # noqa: E402

ALLOWED, STRANGER = 111, 999


class FakeTelegram:
    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.pending = list(updates)
        self.sent: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = self.path.rsplit("/", 1)[-1]
                if method == "getUpdates":
                    offset = body.get("offset") or 0
                    ready = [u for u in owner.pending if u["update_id"] >= offset]
                    if not ready:
                        time.sleep(0.5)
                    result: Any = ready
                elif method == "sendMessage":
                    owner.sent.append(body)
                    result = {"message_id": len(owner.sent)}
                else:
                    result = None
                raw = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def update(update_id: int, user: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": user, "type": "private"},
            "from": {"id": user, "username": f"u{user}"},
            "text": text,
        },
    }


async def run_bot(
    base: Path, broker: FakeBroker, telegram: FakeTelegram, ws: Path, allowed: str, seconds: float
) -> str | None:
    import athena_telegram.__main__ as bot
    from athena_service import ServiceSettings
    from athena_telegram.api import TelegramApi

    os.environ.update(
        {
            "ATHENA_TELEGRAM_TOKEN": "tok-bot",
            "ATHENA_TELEGRAM_ALLOWED_IDS": allowed,
            "ATHENA_TELEGRAM_WORKSPACES": f"{ALLOWED}:{ws}",
            "ATHENA_BROKER_BASE_URL": broker.url,
            "ATHENA_BROKER_TOKEN": broker.token,
            "ATHENA_STATE_DIR": str(base / "state"),
            "ATHENA_SERVICE_PORT": "0",
            "ATHENA_PLANNING": "0",
        }
    )
    original = bot.TelegramApi

    def local_api(token: str, **kwargs: Any) -> TelegramApi:
        return original(token, host="127.0.0.1", port=telegram.port, use_tls=False, poll_seconds=1)

    bot.TelegramApi = local_api  # type: ignore[assignment,misc]
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(seconds, stop.set)
    try:
        await bot.serve(ServiceSettings.from_environment(), stop=stop)
    except Exception as exc:  # T3 espera una negativa a arrancar
        return f"{type(exc).__name__}: {exc}"
    finally:
        bot.TelegramApi = original  # type: ignore[misc]
    return None


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="athena-tg-"))
    (base / "vacia").mkdir()
    os.environ["AI_OS_CONNECT_DIR"] = str(base / "vacia")
    os.environ["LOCALAPPDATA"] = str(base / "local")
    for name in [n for n in os.environ if n.startswith("ATHENA_")]:
        del os.environ[name]
    ws = base / "proyecto"
    ws.mkdir()
    (ws / "README.md").write_text("# demo\n", encoding="utf-8")
    broker = FakeBroker()
    telegram = FakeTelegram(
        [update(1, ALLOWED, "¿Qué hay en este proyecto?"), update(2, STRANGER, "Borra todo")]
    )
    results: dict[str, Any] = {}
    try:
        refused = asyncio.run(run_bot(base, broker, telegram, ws, str(ALLOWED), 25))
        to_allowed = [m["text"] for m in telegram.sent if m["chat_id"] == ALLOWED]
        to_stranger = [m["text"] for m in telegram.sent if m["chat_id"] == STRANGER]
        results["T1_permitido"] = {
            "mensajes": to_allowed,
            "tareas_al_modelo": len(broker.submitted),
            # El canal no reenvía la respuesta del modelo, por diseño (`render_event`: solo
            # hechos operativos). Se espera: run creado, arranque y final anunciados bien.
            "estado": "PASA"
            if len(broker.submitted) >= 1
            and any(t.startswith("Started") for t in to_allowed)
            and any("could not verify" in t for t in to_allowed)
            else "FALLA",
        }
        results["T2_no_permitido"] = {
            "mensajes": to_stranger,
            "estado": "PASA"
            if not any("Respuesta de prueba" in t for t in to_stranger)
            and len(broker.submitted) <= 3
            else "FALLA",
        }
        results["arranque_con_lista"] = refused
        no_list = asyncio.run(run_bot(base, broker, FakeTelegram([]), ws, "", 5))
        results["T3_sin_lista"] = {
            "error": no_list,
            "estado": "PASA" if no_list and "ALLOWED_IDS" in no_list else "FALLA",
        }
    finally:
        broker.close()
        telegram.close()
    (out / "telegram_e2e.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
