"""Ciclo de una conexion: leer, autorizar, enrutar y escribir.

El parser es propio porque Athena no declara dependencias, asi que aqui es
donde se acotan los limites y donde una peticion mal formada se corta antes
de llegar a ningun endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac

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
from athena.errors import (
    AthenaRuntimeError,
    GoalConflict,
    ToolResultUnavailableError,
    ToolValidationError,
    WorkspaceBoundaryError,
    WorkspacePathNotFoundError,
)


class TransporteMixin:
    """Aceptacion de conexiones y enrutado hacia el endpoint."""

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
        self._server = await asyncio.start_server(self._handle, self.config.host, self.config.port)
        socket_name = self._server.sockets[0].getsockname()
        return str(socket_name[0]), int(socket_name[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for task in tuple(self._connections):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await self.registry.shutdown()

    # -- transport --------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            request = await self._read_request(reader)
            if request is None:
                return
            if not self._authorised(request):
                await self._write(writer, Response(401, error_to_json("unauthorized", "Bad token")))
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
        head = await reader.readuntil(b"\r\n\r\n")
        if len(head) > _MAX_HEADER_BYTES:
            return None
        lines = head.decode("latin-1").split("\r\n")
        method, _, rest = lines[0].partition(" ")
        target = rest.rpartition(" ")[0] or "/"
        path, _, raw_query = target.partition("?")
        headers = {}
        for line in lines[1:]:
            if not line.strip():
                continue
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        if length > _MAX_BODY_BYTES:
            return None
        body = await reader.readexactly(length) if length else b""
        return Request(method.upper(), path, _parse_query(raw_query), headers, body)

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
