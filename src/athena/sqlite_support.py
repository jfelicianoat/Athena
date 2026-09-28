"""Conexiones SQLite que se cierran y esquemas que dicen su version.

`with sqlite3.connect(...) as c` confirma o deshace la transaccion, pero **no cierra** la
conexion: queda abierta hasta que el recolector la encuentre. En Windows eso deja el
fichero bloqueado —la auditoria vio `memory.db` y `sessions.db` abiertos al borrar una
carpeta temporal— y hace depender la liberacion de un recurso del GC (A22).

Y ninguna base guardaba su version: una base escrita por un Athena mas nuevo se leia a
medias sin aviso. `migrate` la registra en `PRAGMA user_version` y rechaza lo que no
entiende.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from types import TracebackType

from athena.errors import AthenaRuntimeError


class SchemaVersionError(AthenaRuntimeError):
    code = "schema_version_error"


class closing_connection:
    """Contexto de una conexion: confirma si todo fue bien, deshace si no, y cierra."""

    def __init__(
        self, database: Path | str, *, foreign_keys: bool = False, wal: bool = True
    ) -> None:
        self.database = database
        self.foreign_keys = foreign_keys
        self.wal = wal
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            if self.wal:
                connection.execute("PRAGMA journal_mode=WAL")
            if self.foreign_keys:
                connection.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            connection.close()
            raise
        self._connection = connection
        return connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            if exc_type is None:
                connection.commit()
            else:
                connection.rollback()
        finally:
            connection.close()


def migrate(connection: sqlite3.Connection, schema: str, version: int, name: str) -> None:
    """Crear o poner al dia el esquema y dejar constancia de su version.

    Los esquemas de Athena son aditivos (`CREATE ... IF NOT EXISTS`), asi que aplicar el
    guion sobre una base vieja la pone al dia. Una base con version **mayor** es de un
    Athena posterior: leerla podria perder columnas que este codigo no conoce, y se
    rechaza con un error que lo dice.
    """
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > version:
        raise SchemaVersionError(
            f"The {name} database was written by a newer Athena (schema {current}, this "
            f"version understands up to {version}). Upgrade Athena or use another state "
            "directory."
        )
    connection.executescript(schema)
    if current != version:
        connection.execute(f"PRAGMA user_version = {int(version)}")
    connection.commit()


def process_alive(pid: int) -> bool:
    """Si un proceso de esta maquina sigue vivo. Ante la duda, se dice que si.

    Decir «vivo» de uno muerto deja una sesion sin recuperar hasta que caduque; decir
    «muerto» de uno vivo le roba el trabajo a otra instancia. Lo primero es el error
    barato, asi que es hacia donde se inclina.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # 87 = parametro incorrecto: no existe ese pid. Cualquier otro fallo (acceso
            # denegado) significa que existe y no es nuestro.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


__all__ = ["SchemaVersionError", "closing_connection", "migrate", "process_alive"]
