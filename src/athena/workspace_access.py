"""Quien puede leer y quien puede escribir en una carpeta a la vez.

Antes cada `GraphExecutor` tenia su propio cerrojo de escritura y los lectores iban por
otro semaforo, asi que un explorer leia a mitad de una edicion del coder, y dos grafos (o
dos runs) sobre la misma carpeta escribian a la vez: cada uno se serializaba solo consigo
mismo (auditoria A09).

Ahora hay **un** cerrojo lector/escritor por raiz canonica y por proceso. Lo toman el
ejecutor de grafos (lectura para explorer y verifier, escritura para el coder), cada
herramienta que no este ya dentro de uno, y la verificacion, que tiene que juzgar un
estado quieto y no uno a medio escribir.

Reentrante por tarea: quien ya tiene el cerrojo de una raiz (en cualquier modo) no lo
vuelve a pedir. Sin eso, el coder que tiene la escritura se bloquearia a si mismo al
llamar a `write_file`. Se sabe por una `ContextVar`, que las tareas hijas heredan.

Limite declarado: coordina dentro de un proceso. Dos procesos de Athena sobre la misma
carpeta (el servicio y el escritorio a la vez) no se ven entre si.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import AsyncIterator
from contextvars import ContextVar
from pathlib import Path

from athena.workspace import project_identity

#: Las raices cuyo cerrojo tiene ya la tarea actual.
_HELD: ContextVar[frozenset[str]] = ContextVar("athena_workspace_access_held", default=frozenset())


class ReadWriteLock:
    """Muchos lectores o un escritor. Da preferencia al escritor que espera.

    Sin preferencia, un goteo de lectores (los explorers de un plan grande) podria dejar
    al coder esperando para siempre.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextlib.asynccontextmanager
    async def reading(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writer and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def writing(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(lambda: not self._writer and self._readers == 0)
            finally:
                self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()

    @property
    def readers(self) -> int:
        return self._readers

    @property
    def writer(self) -> bool:
        return self._writer


class WorkspaceAccess:
    """Los cerrojos de todas las carpetas que este proceso esta tocando."""

    def __init__(self) -> None:
        # Por bucle de eventos, y debil: un `asyncio.Condition` pertenece al bucle en que
        # se usa, las pruebas crean muchos, y un bucle cerrado no debe dejar cerrojos.
        self._locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, ReadWriteLock]
        ] = weakref.WeakKeyDictionary()

    def lock_for(self, root: Path) -> ReadWriteLock:
        per_loop = self._locks.setdefault(asyncio.get_running_loop(), {})
        identity = project_identity(root)
        lock = per_loop.get(identity)
        if lock is None:
            lock = ReadWriteLock()
            per_loop[identity] = lock
        return lock

    @contextlib.asynccontextmanager
    async def hold(self, root: Path, *, write: bool) -> AsyncIterator[None]:
        """Tomar la carpeta en el modo pedido, salvo que esta tarea ya la tenga."""
        identity = project_identity(root)
        held = _HELD.get()
        if identity in held:
            yield
            return
        lock = self.lock_for(root)
        mode = lock.writing() if write else lock.reading()
        async with mode:
            token = _HELD.set(held | {identity})
            try:
                yield
            finally:
                _HELD.reset(token)


#: El coordinador del proceso. Uno solo: dos instancias no se coordinarian entre si.
WORKSPACE_ACCESS = WorkspaceAccess()


__all__ = ["WORKSPACE_ACCESS", "ReadWriteLock", "WorkspaceAccess"]
