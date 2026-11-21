"""El modo texto accesible (A11Y-2) de extremo a extremo, contra el Broker falso.

Las respuestas de la persona van guionizadas; todo lo demás es real (runtime, permisos,
verificación, historial, deshacer). Se guarda la transcripción: es exactamente lo que
leería un lector de pantalla.

Uso: python modo_texto_e2e.py <carpeta_salida>   (sale con 1 si algo no cuadra)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ATHENA = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ATHENA / "src"))
from dobles import FakeBroker  # noqa: E402


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="athena-texto-"))
    (base / "vacia").mkdir()
    os.environ["AI_OS_CONNECT_DIR"] = str(base / "vacia")
    os.environ["LOCALAPPDATA"] = str(base / "local")
    for name in [n for n in os.environ if n.startswith("ATHENA_")]:
        del os.environ[name]
    project = base / "proyecto"
    project.mkdir()
    (project / "README.md").write_text("# demo\n", encoding="utf-8")
    broker = FakeBroker()

    from athena_desktop.config import SettingsStore
    from athena_desktop.texto import TextApp

    script = [
        "3", str(project),                     # elegir proyecto
        "4", broker.url, "modelo-bueno", "s",  # conexión: URL, modelo, ¿token? sí
        "1", "3",                              # nueva tarea: documentos
        "Escribe la nota [[escribe:nota.txt:hola accesible]]",
        "2", "1", "nota.txt",                  # cambios: preguntar; ejecutar: no; entregable
        "s",                                   # ¿Permitir esta acción una vez?
        "s",                                   # ¿Deshacer los cambios de este trabajo?
        "2", "1",                              # historial → el trabajo
        "0",                                   # salir
    ]
    feed = iter(script)
    lines: list[str] = []

    def read(prompt: str) -> str:
        value = next(feed)
        lines.append(f"{prompt}{value}")
        return value

    app = TextApp(read=read, write=lines.append, secret=lambda _: broker.token,
                  store=SettingsStore(base / "settings.json"), state_dir=base / "state")
    try:
        code = app.run()
    finally:
        broker.close()
    text = "\n".join(lines)
    (out / "modo_texto_transcripcion.txt").write_text(text, encoding="utf-8")
    checks = {
        "conexion_ok": "Bien: Conectado a AI_Broker" in text,
        "pide_permiso_con_contenido": "Athena pide permiso: Escribir el archivo nota.txt" in text
        and "hola accesible" in text,
        "terminado": "Terminado y comprobado" in text,
        "deshecho": "Restaurados: nota.txt" in text and not (project / "nota.txt").exists(),
        "historial": "Documentos" in text and "Escribe la nota" in text,
        "salida_0": code == 0,
    }
    (out / "modo_texto.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(json.dumps(checks, indent=2))
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
