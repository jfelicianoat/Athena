"""A small HTTP/1.1 + SSE server for one local client.

Written against `asyncio.start_server` and nothing else, because Athena's core declares no
dependencies and an adapter that dragged in a web framework would make the runtime
un-installable without one. The surface is deliberately tiny: a handful of localhost routes
serving one desktop application, not a public API.

Security posture, stated rather than assumed:

- it binds to the loopback interface only;
- every route except health requires a bearer token minted at start-up;
- tokens are compared in constant time;
- artifacts are returned **unredacted**, so the token is what protects them.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from athena.adapters.service.approvals import ApprovalRegistry
from athena.adapters.service.projections import (
    WIRE_VERSION,
    error_to_json,
    event_to_json,
    run_summary_to_json,
    session_to_json,
    status_from_json,
)
from athena.adapters.service.runs import RunOptions, RunRegistry, build_workspace
from athena.cancellation import CancellationSource
from athena.errors import (
    AthenaRuntimeError,
    ToolResultUnavailableError,
    ToolValidationError,
    WorkspaceBoundaryError,
)
from athena.identity import IdentityDirectory
from athena.permissions import PermissionDecision
from athena.tools import ToolResultReference
from athena.types import JSONObject
from athena.workspace import Workspace

_logger = logging.getLogger(__name__)

_MAX_HEADER_BYTES = 16 * 1024
_MAX_BODY_BYTES = 4 * 1024 * 1024
_SSE_KEEPALIVE_SECONDS = 15.0

#: How many idempotency keys to remember. A retry that arrives long after the map has
#: turned over is a new request, which is the honest reading of a key nobody kept.
_IDEMPOTENCY_ENTRIES = 256


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 8770
    #: Minted per start, like AI_Broker. Never persisted by the service itself.
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    delivery_timeout_seconds: float | None = None
    approval_timeout_seconds: float | None = None
    #: Optional gate the host application supplies, e.g. ChatyGPT's authorized folders.
    authorized_workspace: Callable[[Path], bool] | None = None
    #: Athena's identity directory. Absent means this deployment has no notion of a person
    #: beyond "the client holding the bearer token", and link codes cannot be minted.
    directory: IdentityDirectory | None = None

    def __post_init__(self) -> None:
        if self.host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(
                "The Athena service binds to the loopback interface only; "
                f"refusing host {self.host!r}"
            )
        if not self.token:
            raise ValueError("The service requires a non-empty token")


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> JSONObject:
        if not self.body:
            return {}
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolValidationError("Request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ToolValidationError("Request body must be a JSON object")
        return payload


@dataclass(frozen=True, slots=True)
class Response:
    status: int = 200
    payload: JSONObject | None = None
    body: bytes = b""
    content_type: str = "application/json"

    def rendered(self) -> tuple[bytes, str]:
        if self.payload is not None:
            return json.dumps(self.payload, ensure_ascii=False).encode("utf-8"), self.content_type
        return self.body, self.content_type


_REASONS = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    410: "Gone",
    413: "Payload Too Large",
    500: "Internal Server Error",
}

Handler = Callable[[Request], Awaitable[Response]]


class AthenaService:
    """Routes localhost HTTP onto the run registry. It holds no agent logic."""

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
        except ToolResultUnavailableError as exc:
            return Response(410, error_to_json(exc.code, exc.message))
        except ToolValidationError as exc:
            return Response(400, error_to_json(exc.code, exc.message))
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

    async def _list_runs(self, request: Request) -> Response:
        raw = request.query.get("status")
        status = status_from_json(raw) if raw else None
        if raw and status is None:
            raise ToolValidationError(f"Unknown status: {raw}")
        records = await self.registry.list(status)
        return Response(200, {"runs": [run_summary_to_json(record) for record in records]})

    async def _start_run(self, request: Request) -> Response:
        """Create a run and return its id, without waiting for it to do anything.

        The request is over in milliseconds. Holding it open for the length of an agent
        run would tie the work's lifetime to a socket's, and sockets die for reasons that
        have nothing to do with the work — a sleeping laptop, a proxy's idle timeout, a
        client that was restarted. Progress arrives on the event stream instead.
        """
        payload = request.json()
        objective = payload.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise ToolValidationError("objective must be a non-empty string")
        root = payload.get("workspace")
        if not isinstance(root, str) or not root:
            raise ToolValidationError("workspace must be a path")
        key = request.headers.get("idempotency-key", "").strip()
        if key:
            return await self._idempotent_start(key, request, objective, root, payload)
        return await self._create_run(objective, root, payload, status=201)

    async def _idempotent_start(
        self,
        key: str,
        request: Request,
        objective: str,
        root: str,
        payload: JSONObject,
    ) -> Response:
        """Create at most one run per `Idempotency-Key`.

        A retry is not a second request. Starting a run is expensive and not reversible in
        the way a read is — two agents on one workspace is exactly the outcome a client
        retrying a timed-out POST is trying to avoid.

        The in-flight case is handled with a future rather than a "seen" set, because the
        window that matters is precisely the one where the first call has not finished:
        a check-then-act across `await registry.start(...)` would let both callers miss.
        """
        del request
        existing = self._idempotency.get(key)
        if existing is not None:
            try:
                run_id = await asyncio.shield(existing)
            except asyncio.CancelledError:
                if not existing.cancelled():
                    # This request is being cancelled, not the run it was waiting on.
                    raise
                # The call we were waiting on failed and withdrew its key. Waiting for a
                # run that will never exist would be a worse answer than doing the work.
                existing = None
            if existing is not None:
                return Response(
                    200,
                    {
                        "run_id": run_id,
                        "idempotent_replay": True,
                        "workspace_id": Workspace.from_path(root).workspace_id,
                    },
                )

        pending: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._idempotency[key] = pending
        try:
            response = await self._create_run(objective, root, payload, status=201)
            created = (response.payload or {}).get("run_id")
            if not isinstance(created, str):
                raise AthenaRuntimeError("A created run has no id to be idempotent about")
        except BaseException:
            # A failed attempt must not become a cached answer: the caller is entitled to
            # retry the same key and actually get a run. Withdrawing the key *and*
            # cancelling the future is what lets a concurrent waiter fall through above,
            # instead of blocking on a promise nobody is going to keep.
            self._idempotency.pop(key, None)
            if not pending.done():
                pending.cancel()
            raise
        pending.set_result(created)
        self._trim_idempotency()
        return response

    def _trim_idempotency(self) -> None:
        """Keep the map bounded. A retry that arrives an hour later is a new request."""
        while len(self._idempotency) > _IDEMPOTENCY_ENTRIES:
            self._idempotency.pop(next(iter(self._idempotency)))

    async def _create_run(
        self, objective: str, root: str, payload: JSONObject, *, status: int
    ) -> Response:
        workspace = build_workspace(root, self.config.authorized_workspace)
        options = RunOptions.from_json(payload)
        run_id = await self.registry.start(objective, workspace, options)
        return Response(
            status,
            {
                "run_id": run_id,
                "workspace_id": workspace.workspace_id,
                "writes": options.writes.value,
                "exec": options.execution.value,
            },
        )

    async def _get_run(self, run_id: str) -> Response:
        record = await self.registry.snapshot(run_id)
        if record is None:
            return Response(404, error_to_json("not_found", f"Unknown run: {run_id}"))
        return Response(200, session_to_json(record))

    async def _metrics(self) -> Response:
        """Lo medido hasta ahora, agregado y comparado por estrategia.

        Agregados y no la lista de runs: quien pregunta quiere saber si descomponer sale a
        cuenta, y devolver cada run haría que la respuesta creciera con el uso hasta ser
        inservible por su propio tamaño.
        """
        if self.registry.metrics_store is None:
            return Response(
                404, error_to_json("metrics_disabled", "This deployment records no metrics")
            )
        return Response(200, await self.registry.metrics_store.compare())

    async def _resume_run(self, run_id: str, request: Request) -> Response:
        payload = request.json()
        root = payload.get("workspace")
        if not isinstance(root, str) or not root:
            raise ToolValidationError("workspace must be a path")
        workspace = build_workspace(root, self.config.authorized_workspace)
        resumed = await self.registry.resume(run_id, workspace)
        return Response(202, {"run_id": resumed, "resumed": True})

    def _acknowledge(self, run_id: str, request_id: str) -> Response:
        window = (
            self.config.approval_timeout_seconds
            if self.config.approval_timeout_seconds is not None
            else 300.0
        )
        pending = self.approvals.acknowledge(request_id, window)
        if pending is None or pending.run_id != run_id:
            return Response(404, error_to_json("not_found", "No such approval request"))
        return Response(200, pending.to_json())

    def _decide(self, run_id: str, request_id: str, request: Request) -> Response:
        payload = request.json()
        raw = payload.get("decision")
        if raw not in ("allow", "deny"):
            raise ToolValidationError("decision must be 'allow' or 'deny'")
        subscriber_id = request.headers.get("x-athena-subscriber")
        if not self.registry.controls(run_id, subscriber_id):
            return Response(
                403,
                error_to_json(
                    "not_controller",
                    "Another client controls this run; observers may not approve",
                ),
            )
        existing = self.approvals.get(request_id)
        if existing is None or existing.run_id != run_id:
            return Response(404, error_to_json("not_found", "No such approval request"))
        if existing.consumed:
            # Single use: a replayed POST must not approve a second action.
            return Response(409, error_to_json("already_resolved", "This request was answered"))
        decision = PermissionDecision.ALLOW if raw == "allow" else PermissionDecision.DENY
        self.approvals.resolve(request_id, decision)
        return Response(200, {"request_id": request_id, "decision": decision.value})

    def _directory(self) -> IdentityDirectory:
        directory = self.config.directory
        if directory is None:
            raise ToolValidationError("This Athena service has no identity directory")
        return directory

    async def _create_user(self, request: Request) -> Response:
        payload = request.json()
        raw = payload.get("display_name")
        display_name = raw.strip() if isinstance(raw, str) and raw.strip() else None
        user = await self._directory().create_user(display_name)
        return Response(201, {"user_id": user.user_id, "display_name": user.display_name})

    async def _issue_link_code(self, request: Request) -> Response:
        """Mint a code for a user the caller says it is acting for.

        The caller is believed because it already holds the bearer token for a
        loopback-only service — this is not a second authentication step, and pretending
        otherwise would be worse than saying so. That belief is bounded rather than
        trusted: the code it produces is single-use and dies within minutes, so a client
        that asks for the wrong user has minted a mistake with a short life rather than a
        standing grant.

        The plaintext appears in this response and nowhere else, ever.
        """
        payload = request.json()
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ToolValidationError("user_id is required")
        token = await self._directory().issue_link_token(user_id.strip())
        return Response(
            201,
            {
                "token_id": token.token_id,
                "code": token.code,
                "user_id": token.user_id,
                "expires_at": token.expires_at.isoformat(),
            },
        )

    async def _list_links(self, user_id: str) -> Response:
        links = await self._directory().links_for(user_id)
        return Response(
            200,
            {
                "links": [
                    {
                        "identity_key": link.identity_key,
                        "channel": link.channel,
                        "linked_at": link.linked_at.isoformat(),
                    }
                    for link in links
                ]
            },
        )

    async def _artifact(self, key: str) -> Response:
        reference = ToolResultReference(store_key=key, media_type="text/plain", size_chars=0)
        content = await self.registry.result_store.get(reference, CancellationSource().token)
        return Response(200, body=content.encode("utf-8"), content_type="text/plain; charset=utf-8")

    # -- server-sent events -----------------------------------------------

    async def _stream_events(self, request: Request, writer: asyncio.StreamWriter) -> None:
        """Snapshot first, then the live tail — ADR-017 §5."""
        run_id = _match(request.path, "/v1/runs/{}/events") or ""
        wants_control = request.query.get("control") == "1"
        try:
            subscriber = self.registry.subscribe(run_id, control=wants_control)
        except AthenaRuntimeError as exc:
            await self._write(writer, Response(404, error_to_json(exc.code, exc.message)))
            return

        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        try:
            # Subscribed already, so anything published from here on is queued. The replay
            # is read *after* subscribing and de-duplicated against the queue below;
            # reading it first would leave a gap exactly the width of the snapshot read.
            resume_from = _last_event_id(request)
            missed = self.registry.replay(run_id, resume_from) if resume_from is not None else None
            replayed: set[str] = set()

            if missed is not None:
                # The client is close enough behind to be caught up event by event, so it
                # keeps whatever it had derived rather than throwing it away and
                # rebuilding from a snapshot it did not ask for.
                await _send(
                    writer,
                    "state",
                    {
                        "subscriber_id": subscriber.subscriber_id,
                        "controls": subscriber.controls,
                        "wire_version": WIRE_VERSION,
                        "resumed": True,
                        "shape": self.registry.shape_of(run_id),
                        "snapshot": None,
                        "pending_approvals": [
                            pending.to_json() for pending in self.approvals.pending_for(run_id)
                        ],
                    },
                )
                for missed_event in missed:
                    replayed.add(missed_event.event_id)
                    await _send(
                        writer,
                        "event",
                        event_to_json(missed_event),
                        event_id=missed_event.event_id,
                    )
            else:
                record = await self.registry.snapshot(run_id)
                await _send(
                    writer,
                    "state",
                    {
                        "subscriber_id": subscriber.subscriber_id,
                        "controls": subscriber.controls,
                        "wire_version": WIRE_VERSION,
                        "resumed": False,
                        "shape": self.registry.shape_of(run_id),
                        "snapshot": session_to_json(record) if record else None,
                        "pending_approvals": [
                            pending.to_json() for pending in self.approvals.pending_for(run_id)
                        ],
                    },
                )

            while True:
                try:
                    event = await asyncio.wait_for(
                        subscriber.queue.get(), timeout=_SSE_KEEPALIVE_SECONDS
                    )
                except TimeoutError:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    continue
                if event is None:
                    break
                if event.event_id in replayed:
                    # Queued during the replay window. Sending it twice would make a
                    # client that counts things count them twice.
                    continue
                await _send(writer, "event", event_to_json(event), event_id=event.event_id)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.registry.unsubscribe(subscriber)


def _last_event_id(request: Request) -> str | None:
    """Where a reconnecting client says it got to.

    The header is what the SSE specification defines and what a browser sends by itself on
    reconnect. The query parameter exists for clients that cannot set headers on the
    request that opens the stream, which is more of them than one would hope.
    """
    header = request.headers.get("last-event-id")
    if header and header.strip():
        return header.strip()
    query = request.query.get("last_event_id")
    return query.strip() if query and query.strip() else None


async def _send(
    writer: asyncio.StreamWriter,
    name: str,
    payload: Any,
    *,
    event_id: str | None = None,
) -> None:
    frame = ""
    if event_id:
        frame += f"id: {event_id}\n"
    frame += f"event: {name}\n"
    frame += f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
    writer.write(frame.encode("utf-8"))
    await writer.drain()


def _parse_query(raw: str) -> dict[str, str]:
    return dict(parse_qsl(raw, keep_blank_values=True))


def _match(path: str, template: str) -> str | None:
    """One-placeholder route match. Enough for this surface, and obvious to read."""
    prefix, _, suffix = template.partition("{}")
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    middle = path[len(prefix) : len(path) - len(suffix) if suffix else None]
    if not middle or "/" in middle:
        return None
    return middle


def _match2(path: str, template: str) -> tuple[str, str] | None:
    first, _, rest = template.partition("{}")
    second, _, tail = rest.partition("{}")
    if not path.startswith(first):
        return None
    remainder = path[len(first) :]
    left, _, right = remainder.partition(second)
    if not left or "/" in left:
        return None
    if tail:
        if not right.endswith(tail):
            return None
        right = right[: len(right) - len(tail)]
    if not right or "/" in right:
        return None
    return left, right


__all__ = ["AthenaService", "Request", "Response", "ServiceConfig"]
