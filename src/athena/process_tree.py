"""Matar un proceso con todo lo que lanzo, y saber si de verdad murio.

`taskkill /T` recorre el arbol por el pid del padre en el momento de llamarlo: un nieto
cuyo padre ya termino queda fuera, y el codigo de salida de `taskkill` se ignoraba, asi
que se anunciaba la terminacion sin comprobarla (auditoria A14).

En Windows cada hijo entra en un **Job Object** con `KILL_ON_JOB_CLOSE`. Los procesos que
lanza heredan el job, de modo que terminarlo alcanza a toda la descendencia aunque la
cadena de padres se haya roto. `taskkill` queda como respaldo y su resultado cuenta. En
POSIX el hijo encabeza su propio grupo de procesos y se mata el grupo.

Queda una ventana: lo que el hijo lance entre su arranque y su entrada en el job no
hereda el job (Python no permite crear el proceso suspendido). El respaldo de
`taskkill` cubre ese caso mientras la cadena de padres exista.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import weakref

from athena.errors import ToolExecutionError

#: Cuanto se espera a que el arbol desaparezca tras ordenar su muerte.
REAP_TIMEOUT_SECONDS = 10.0


class ProcessTreeError(ToolExecutionError):
    code = "process_tree_not_terminated"


if sys.platform == "win32":  # pragma: no cover - rama por plataforma
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        )

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    class _Job:
        """Un job de Windows que contiene al hijo y a lo que lance."""

        def __init__(self, pid: int) -> None:
            self.handle: int | None = None
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            configured = _kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
            process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            assigned = bool(process) and bool(_kernel32.AssignProcessToJobObject(job, process))
            if process:
                _kernel32.CloseHandle(process)
            if not (configured and assigned):
                _kernel32.CloseHandle(job)
                return
            self.handle = job

        def terminate(self) -> bool:
            if self.handle is None:
                return False
            return bool(_kernel32.TerminateJobObject(self.handle, 1))

        def close(self) -> None:
            if self.handle is not None:
                _kernel32.CloseHandle(self.handle)
                self.handle = None

        def __del__(self) -> None:
            with contextlib.suppress(Exception):
                self.close()

else:

    class _Job:
        def __init__(self, pid: int) -> None:
            del pid
            self.handle: int | None = None

        def terminate(self) -> bool:
            return False

        def close(self) -> None:
            return None


_JOBS: weakref.WeakKeyDictionary[asyncio.subprocess.Process, _Job] = weakref.WeakKeyDictionary()


class ProcessTreeHandle:
    """El arbol de un proceso lanzado con `subprocess` (no asyncio), para poder matarlo entero.

    Lo usa el escritorio para el servicio que gestiona: en Windows el `pythonw.exe` de un
    entorno virtual arranca el interprete real como hijo, y es el hijo quien abre el
    puerto. Terminar solo el pid guardado dejaba un huerfano con el puerto ocupado.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._job = _Job(pid)

    @property
    def contained(self) -> bool:
        return self._job.handle is not None

    def kill(self) -> bool:
        """Terminar el arbol. True si alguna via lo confirmo."""
        confirmed = self._job.terminate()
        if sys.platform == "win32":
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                completed = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                confirmed = confirmed or completed.returncode in (0, 128)
        else:
            with contextlib.suppress(OSError):
                os.killpg(self.pid, signal.SIGKILL)
                confirmed = True
        return confirmed

    def close(self) -> None:
        self._job.close()


def contain(process: asyncio.subprocess.Process) -> None:
    """Meter un hijo recien lanzado en su job. Sin job, queda el respaldo de taskkill."""
    if sys.platform != "win32":
        return
    job = _Job(process.pid)
    if job.handle is not None:
        _JOBS[process] = job


def release(process: asyncio.subprocess.Process) -> None:
    """El hijo termino: cerrar su job mata lo que dejase vivo detras."""
    job = _JOBS.pop(process, None)
    if job is not None:
        job.close()


def terminate_tree(process: asyncio.subprocess.Process) -> bool:
    """Ordenar la muerte del hijo y de su descendencia. True si alguna via lo confirmo.

    No espera: quien llama tiene que `reap` para saber si de verdad termino.
    """
    confirmed = False
    if sys.platform == "win32":
        # El job aunque el hijo ya haya terminado: lo que queda dentro es justo la
        # descendencia que sobrevivio a su padre.
        job = _JOBS.get(process)
        if job is not None:
            confirmed = job.terminate()
        if process.returncode is None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                completed = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                # 0: terminado. 128: ya no existia, que para esto es lo mismo.
                confirmed = confirmed or completed.returncode in (0, 128)
    else:
        # El hijo encabeza su grupo (`start_new_session`), asi que el grupo es su pid
        # tambien cuando el ya no existe y quedan nietos en el.
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
            confirmed = True
    if process.returncode is not None:
        return True
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
    return confirmed


async def reap(
    process: asyncio.subprocess.Process, *, timeout: float = REAP_TIMEOUT_SECONDS
) -> None:
    """Esperar a que el hijo termine de verdad, con un plazo.

    Antes se esperaba sin limite: un arbol que no moria dejaba colgado al que cancelaba.
    Ahora, pasado el plazo, se dice que el cierre no se completo.
    """
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        raise ProcessTreeError(
            f"The process {process.pid} did not terminate within {timeout:g} s after it "
            "was killed; it may still be running",
            details={"pid": process.pid},
        ) from None
    finally:
        if process.returncode is not None:
            release(process)


#: Cuanto se espera a que la salida termine de llegar cuando el hijo ya salio.
DRAIN_GRACE_SECONDS = 2.0


#: Cuanto se conserva de cada tuberia: el principio y el final, hasta este tamano cada
#: uno. Lo de en medio se lee y se tira. La herramienta recorta a 20 000 caracteres y la
#: verificacion mira la cola; guardar gigas para quedarse con eso era gastar memoria en
#: nada (A21).
CAPTURE_EDGE_BYTES = 1024 * 1024


async def _drain(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    head = bytearray()
    tail = bytearray()
    dropped = 0
    while chunk := await stream.read(65536):
        room = CAPTURE_EDGE_BYTES - len(head)
        if room > 0:
            head.extend(chunk[:room])
            chunk = chunk[room:]
        if chunk:
            tail.extend(chunk)
            if len(tail) > CAPTURE_EDGE_BYTES:
                excess = len(tail) - CAPTURE_EDGE_BYTES
                dropped += excess
                del tail[:excess]
    if dropped:
        marker = f"\n[... {dropped} bytes omitted ...]\n".encode()
        return bytes(head) + marker + bytes(tail)
    return bytes(head) + bytes(tail)


async def _capture(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    stdout, stderr = await asyncio.gather(_drain(process.stdout), _drain(process.stderr))
    # Como `communicate()`: con las tuberias cerradas, esperar tambien al proceso para que
    # el codigo de salida este puesto.
    await process.wait()
    return stdout, stderr


async def _exit_of(process: asyncio.subprocess.Process) -> None:
    """Cuando termina el hijo directo, no cuando se cierran sus tuberias.

    `process.wait()` no sirve para esto: desde Python 3.12 no se resuelve hasta que se
    cierran tambien los pipes, que es justo lo que un nieto impide. El codigo de salida
    se fija en cuanto el proceso termina.
    """
    while process.returncode is None:
        await asyncio.sleep(0.05)


async def communicate_bounded(
    process: asyncio.subprocess.Process, timeout: float
) -> tuple[bytes, bytes]:
    """`communicate()` que no se queda esperando a los nietos.

    Un proceso de fondo lanzado por el comando hereda sus tuberias: `communicate()` espera
    a que se cierren, es decir, a que termine el nieto, y un comando que acabo en un
    segundo colgaba la herramienta hasta su plazo. Cuando el hijo directo termina se da
    un margen para vaciar la salida y, si su descendencia sigue sujetandola, se termina.

    Lanza `TimeoutError` si el hijo no termina dentro de `timeout`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    output = asyncio.ensure_future(_capture(process))
    exited = asyncio.ensure_future(_exit_of(process))
    try:
        done, _ = await asyncio.wait(
            {output, exited}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if output in done:
            return output.result()
        if exited not in done:
            raise TimeoutError
        grace = max(0.0, min(DRAIN_GRACE_SECONDS, deadline - loop.time()))
        try:
            return await asyncio.wait_for(asyncio.shield(output), timeout=grace)
        except TimeoutError:
            terminate_tree(process)
            return await asyncio.wait_for(output, timeout=REAP_TIMEOUT_SECONDS)
    finally:
        for future in (output, exited):
            if not future.done():
                future.cancel()


__all__ = [
    "CAPTURE_EDGE_BYTES",
    "DRAIN_GRACE_SECONDS",
    "REAP_TIMEOUT_SECONDS",
    "ProcessTreeError",
    "ProcessTreeHandle",
    "communicate_bounded",
    "contain",
    "reap",
    "release",
    "terminate_tree",
]
