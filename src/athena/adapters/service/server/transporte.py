"""Ciclo de una conexion: leer, autorizar, enrutar y escribir.

El parser es propio porque Athena no declara dependencias, asi que aqui es
donde se acotan los limites y donde una peticion mal formada se corta antes
de llegar a ningun endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from typing import TYPE_CHECKING

from athena.adapters.service.approvals import ApprovalRegistry
from athena.adapters.service.projections import (
    WIRE_VERSION,
    error_to_json,
)
from athena.adapters.service.runs import RunRegistry
from athena.adapters.service.server.auxiliares import (
    _match,
    _match2,
    _parse_query,
)
from athena.adapters.service.server.http import (
    _MAX_BODY_BYTES,
    _MAX_HEADER_BYTES,
    _REASONS,
    Request,
    Response,
    ServiceConfig,
    _logger,
)
from athena.cancellation import CancellationSource
from athena.errors import (
    AthenaRuntimeError,
    GoalConflict,
    ToolResultUnavailableError,
    ToolValidationError,
    WorkspaceBoundaryError,
    WorkspacePathNotFoundError,
)

#: Plazos de lectura. Sin ellos una conexion que manda la cabecera a trocitos, o que
#: anuncia un cuerpo y no lo manda, retiene su tarea indefinidamente.
_HEADER_TIMEOUT = 10.0
_BODY_TIMEOUT = 30.0
#: Conexiones abiertas a la vez. El servicio es loopback, pero sin techo un cliente con
#: errores puede agotar descriptores y dejar a los demas sin servicio.
_MAX_CONNECTIONS = 64


class _BadRequest(Exception):
    """Una peticion que se rechaza con una respuesta concreta."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class TransporteMixin:
    """Aceptacion de conexiones y enrutado hacia el endpoint."""

    if TYPE_CHECKING:
        # El contrato con los mixins de endpoints, dicho para el comprobador de tipos. El
        # enrutado vive aqui y los manejadores alli; sin esta declaracion mypy no puede
        # saber que existen, y el refactor de septiembre dejo 44 errores por eso (A24).
        async def _memory(self, request: Request) -> Response: ...
        async def _confirm_memory(self, item_id: str) -> Response: ...
        async def _forget_memory(self, item_id: str) -> Response: ...
        async def _metrics(self) -> Response: ...
        async def _list_runs(self, request: Request) -> Response: ...
        async def _start_run(self, request: Request) -> Response: ...
        async def _get_run(self, run_id: str) -> Response: ...
        async def _history(self, run_id: str, request: Request) -> Response: ...
        def _revise_goal(self, run_id: str, request: Request) -> Response: ...
        def _rollback_points(self, run_id: str) -> Response: ...
        async def _rollback(self, run_id: str, request: Request) -> Response: ...
        async def _resume_run(self, run_id: str, request: Request) -> Response: ...
        def _acknowledge(self, run_id: str, request_id: str) -> Response: ...
        def _decide(self, run_id: str, request_id: str, request: Request) -> Response: ...
        async def _artifact(self, key: str) -> Response: ...
        async def _create_user(self, request: Request) -> Response: ...
        async def _issue_link_code(self, request: Request) -> Response: ...
        async def _list_links(self, user_id: str) -> Response: ...
        async def _stream_events(self, request: Request, writer: asyncio.StreamWriter) -> None: ...

    def __init__(self, registry: RunRegistry, config: ServiceConfig | None = None) -> None:
        self.registry = registry
        self.config = config or ServiceConfig()
        self.approvals: ApprovalRegistry = registry.approvals
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        #: Idempotency key to the run it created, or to the call still creating it.
        self._idempotency: dict[str, asyncio.Future[str]] = {}

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> tuple[str, int]:
        """Open the port. Interrupted runs are marked before anyone can observe them."""
        await self.registry.mark_interrupted()
        if self.registry.system1 is not None:
            await self.registry.system1.initialize(CancellationSource().token)
        self._server = await asyncio.start_server(self._handle, self.config.host, self.config.port)
        socket_name = self._server.sockets[0].getsockname()
        return str(socket_name[0]), int(socket_name[1])

    async def stop(self) -> None:
        # Las conexiones se cancelan antes de esperar al servidor: desde Python 3.12
        # `wait_closed` espera a que terminen todos los manejadores, y un stream SSE cuyo
        # cliente ya se fue no se entera hasta el siguiente latido. Parar el servicio
        # tardaba eso en cada cliente inactivo.
        if self._server is not None:
            self._server.close()
        for task in tuple(self._connections):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        await self.registry.shutdown()

    # -- transport --------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            if len(self._connections) > _MAX_CONNECTIONS:
                await self._write(
                    writer, Response(503, error_to_json("busy", "Too many open connections"))
                )
                return
            try:
                request = await self._read_request(reader)
            except _BadRequest as refused:
                # Una peticion mal formada tiene respuesta, no un 500 ni un cierre mudo
                # (A20): el cliente necesita saber que fue su peticion, y cual.
                await self._write(
                    writer, Response(refused.status, error_to_json(refused.code, refused.message))
                )
                return
            if request is None:
                return
            if not self._authorised(request):
                await self._write(writer, Response(401, error_to_json("unauthorized", "Bad token")))
                return
            try:
                request = await self._read_body(reader, request)
            except _BadRequest as refused:
                await self._write(
                    writer, Response(refused.status, error_to_json(refused.code, refused.message))
                )
                return
            if request.method == "GET" and _match(request.path, "/v1/runs/{}/events"):
                await self._stream_events(request, writer)
                return
            response = await self._route(request)
            await self._write(writer, response)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:
            # The class name is an internal fact — `KeyError`, `AttributeError` — and
            # naming it on the wire tells a caller about Athena's insides while telling
            # them nothing they can act on. It goes to the log; the client gets a code.
            _logger.exception("service.unhandled_error")
            with contextlib.suppress(Exception):
                await self._write(
                    writer,
                    Response(
                        500,
                        error_to_json("internal_error", "Athena failed to handle that request"),
                    ),
                )
        finally:
            if task is not None:
                self._connections.discard(task)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _read_request(self, reader: asyncio.StreamReader) -> Request | None:
        """La cabecera, con plazo y con framing estricto. El cuerpo se lee aparte.

        Se separa a proposito: la credencial se comprueba **antes** de leer el cuerpo, asi
        que un cliente sin token no puede hacer que el servicio reserve 4 MiB por
        conexion (A20). `None` cuando el cliente cerro sin mandar nada.
        """
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEADER_TIMEOUT)
        except asyncio.LimitOverrunError:
            raise _BadRequest(431, "headers_too_large", "Request headers are too large") from None
        except asyncio.IncompleteReadError as incomplete:
            if not incomplete.partial:
                return None
            raise _BadRequest(400, "bad_request", "The request ended before its headers") from None
        except TimeoutError:
            raise _BadRequest(408, "request_timeout", "The request headers took too long") from None
        if len(head) > _MAX_HEADER_BYTES:
            raise _BadRequest(431, "headers_too_large", "Request headers are too large")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[0].isalpha() or not parts[2].startswith("HTTP/"):
            raise _BadRequest(400, "bad_request", "Malformed request line")
        method, target = parts[0], parts[1] or "/"
        path, _, raw_query = target.partition("?")
        headers: dict[str, str] = {}
        lengths: set[str] = set()
        for line in lines[1:]:
            if not line.strip():
                continue
            name, separator, value = line.partition(":")
            if not separator or not name.strip() or name != name.strip():
                raise _BadRequest(400, "bad_request", "Malformed header line")
            key = name.strip().lower()
            if key == "content-length":
                lengths.add(value.strip())
            headers[key] = value.strip()
        if "transfer-encoding" in headers:
            # Sin soporte de chunked: aceptarlo junto a un Content-Length es la forma
            # clasica de que dos lectores discrepen sobre donde acaba una peticion.
            raise _BadRequest(400, "unsupported_framing", "Transfer-Encoding is not supported")
        if len(lengths) > 1:
            raise _BadRequest(400, "bad_request", "Conflicting Content-Length headers")
        raw_length = next(iter(lengths), "0") or "0"
        if not raw_length.isdigit():
            raise _BadRequest(400, "bad_request", "Content-Length must be a non-negative integer")
        length = int(raw_length)
        if length > _MAX_BODY_BYTES:
            raise _BadRequest(413, "payload_too_large", "The request body is too large")
        headers["content-length"] = str(length)
        return Request(method.upper(), path, _parse_query(raw_query), headers, b"")

    async def _read_body(self, reader: asyncio.StreamReader, request: Request) -> Request:
        length = int(request.headers.get("content-length", "0"))
        if not length:
            return request
        try:
            body = await asyncio.wait_for(reader.readexactly(length), _BODY_TIMEOUT)
        except asyncio.IncompleteReadError:
            raise _BadRequest(
                400, "bad_request", "The body is shorter than Content-Length"
            ) from None
        except TimeoutError:
            raise _BadRequest(408, "request_timeout", "The request body took too long") from None
        return Request(request.method, request.path, request.query, request.headers, body)

    def _authorised(self, request: Request) -> bool:
        if request.path == "/v1/health":
            return True
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(presented, self.config.token)

    async def _write(self, writer: asyncio.StreamWriter, response: Response) -> None:
        body, content_type = response.rendered()
        reason = _REASONS.get(response.status, "OK")
        head = (
            f"HTTP/1.1 {response.status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()

    # -- routing ----------------------------------------------------------

    async def _route(self, request: Request) -> Response:
        try:
            return await self._dispatch(request)
        except WorkspaceBoundaryError as exc:
            return Response(403, error_to_json("workspace_boundary", exc.message))
        except WorkspacePathNotFoundError as exc:
            # 404, no 403: la ruta esta dentro y no esta. Devolver «prohibido» por algo que
            # solo falta manda a revisar permisos en vez de el nombre del fichero.
            return Response(404, error_to_json(exc.code, exc.message))
        except ToolResultUnavailableError as exc:
            return Response(410, error_to_json(exc.code, exc.message))
        except ToolValidationError as exc:
            return Response(400, error_to_json(exc.code, exc.message))
        except GoalConflict as exc:
            # 409 literal: alguien escribio sobre una version que ya no era la vigente. El
            # cuerpo lleva el objetivo actual para que quien llego tarde decida con el
            # delante en vez de tener que volver a preguntarlo.
            return Response(
                409,
                {
                    "error": {"code": exc.code, "message": exc.message},
                    **exc.details,
                },
            )
        except AthenaRuntimeError as exc:
            return Response(409, error_to_json(exc.code, exc.message))

    async def _dispatch(self, request: Request) -> Response:
        path, method = request.path, request.method
        if path == "/v1/health":
            return Response(
                200,
                {
                    "status": "ok",
                    "wire_version": WIRE_VERSION,
                    "runs": len(self.registry.live_ids()),
                },
            )
        if path == "/v1/memory" and method == "GET":
            return await self._memory(request)
        if (item := _match(path, "/v1/memory/{}/confirm")) and method == "POST":
            return await self._confirm_memory(item)
        if (item := _match(path, "/v1/memory/{}")) and method == "DELETE":
            return await self._forget_memory(item)
        if path == "/v1/profiles" and method == "GET":
            # Que ofrece este despliegue. Sin esto un cliente elige a ciegas, y elegir a
            # ciegas entre perfiles que cambian que herramientas existen y que cuenta como
            # prueba no es elegir: es acertar.
            return Response(
                200,
                {
                    "default": self.registry.profiles.default.name,
                    "profiles": [
                        self.registry.profiles.get(name).to_json()
                        for name in self.registry.profiles.names()
                    ],
                },
            )
        if path == "/v1/models" and method == "GET":
            # Lo mismo que `/v1/profiles` y por el mismo motivo: un selector no puede
            # inventarse las opciones. 404 cuando no hay eleccion que ofrecer, igual que
            # metricas y memoria — «este despliegue no hace eso» no es un fallo.
            if self.registry.models is None:
                return Response(
                    404,
                    error_to_json("models_fixed", "This deployment does not offer a model choice"),
                )
            return Response(200, self.registry.models.to_json())
        if path == "/v1/metrics" and method == "GET":
            return await self._metrics()
        if path == "/v1/auth/check" and method == "GET":
            # Deliberadamente vacío y barato. Sirve para una sola pregunta —«¿vale esta
            # credencial?»— y responderla con datos invitaría a sondearlo por ellos.
            #
            # Existe porque `/v1/health` no puede contestarla: es público a propósito, y
            # un cliente que dedujese de un 200 que está autenticado se anunciaría como
            # conectado mientras todo lo demás le devuelve 401.
            return Response(200, {"authenticated": True, "wire_version": WIRE_VERSION})
        if path == "/v1/runs" and method == "GET":
            return await self._list_runs(request)
        if path == "/v1/runs" and method == "POST":
            return await self._start_run(request)
        if (run_id := _match(path, "/v1/runs/{}")) and method == "GET":
            return await self._get_run(run_id)
        if (run_id := _match(path, "/v1/runs/{}/history")) and method == "GET":
            return await self._history(run_id, request)
        if (run_id := _match(path, "/v1/runs/{}/goal")) and method == "GET":
            return Response(200, self.registry.goal_of(run_id).to_json())
        if (run_id := _match(path, "/v1/runs/{}/goal")) and method == "POST":
            return self._revise_goal(run_id, request)
        if (run_id := _match(path, "/v1/runs/{}/rollback")) and method == "GET":
            return self._rollback_points(run_id)
        if (run_id := _match(path, "/v1/runs/{}/rollback")) and method == "POST":
            return await self._rollback(run_id, request)
        if (run_id := _match(path, "/v1/runs/{}/cancel")) and method == "POST":
            await self.registry.cancel(run_id)
            return Response(202, {"run_id": run_id, "cancelling": True})
        if (run_id := _match(path, "/v1/runs/{}/resume")) and method == "POST":
            return await self._resume_run(run_id, request)
        if (pair := _match2(path, "/v1/runs/{}/approvals/{}/ack")) and method == "POST":
            return self._acknowledge(*pair)
        if (pair := _match2(path, "/v1/runs/{}/approvals/{}")) and method == "POST":
            return self._decide(*pair, request)
        if (key := _match(path, "/v1/results/{}")) and method == "GET":
            return await self._artifact(key)
        if path == "/v1/identity/users" and method == "POST":
            return await self._create_user(request)
        if path == "/v1/identity/link-codes" and method == "POST":
            return await self._issue_link_code(request)
        if (user_id := _match(path, "/v1/identity/users/{}/links")) and method == "GET":
            return await self._list_links(user_id)
        return Response(404, error_to_json("not_found", f"No route for {method} {path}"))

    # -- handlers ---------------------------------------------------------
