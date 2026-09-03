"""Endpoints de metricas, memoria, identidad, artefactos y eventos.

El stream SSE vive aqui porque es un endpoint mas, aunque escriba durante
minutos: quien lo lee tiene que poder reengancharse por `Last-Event-ID`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from athena.adapters.service.projections import (
    WIRE_VERSION,
    error_to_json,
    event_to_json,
    session_to_json,
)
from athena.adapters.service.server.auxiliares import (
    _last_event_id,
    _match,
    _positive_int,
    _send,
)
from athena.adapters.service.server.endpoints_runs import EndpointsRunsMixin
from athena.adapters.service.server.http import (
    _SSE_KEEPALIVE_SECONDS,
    Request,
    Response,
)
from athena.cancellation import CancellationSource
from athena.errors import (
    AthenaRuntimeError,
    ToolValidationError,
)
from athena.identity import IdentityDirectory
from athena.project_memory import SqliteProjectMemory, VerificationState
from athena.tools import ToolResultReference


class EndpointsVariosMixin(EndpointsRunsMixin):
    """Metricas, memoria de proyecto, identidad, artefactos y SSE."""

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

    async def _memory(self, request: Request) -> Response:
        """Lo que Athena cree saber de un proyecto, para que alguien pueda mirarlo.

        Existe porque el escalon mas alto —«una persona lo respalda»— no se puede alcanzar
        sin una persona, y una persona no puede respaldar lo que no ve. Sin esto,
        `USER_CONFIRMED` era un estado inalcanzable con nombre.
        """
        memory = self._memory_store()
        if memory is None:
            return Response(
                404, error_to_json("memory_disabled", "This deployment remembers nothing")
            )
        project_id = request.query.get("project")
        if not project_id:
            raise ToolValidationError("project is required")
        items = await memory.active(
            project_id, limit=_positive_int(request.query.get("limit")) or 50
        )
        ahora = datetime.now(UTC)
        return Response(
            200,
            {
                "project_id": project_id,
                "items": [
                    # La edad va calculada, no en crudo: quien mira esto quiere saber de
                    # que fiarse, y una fecha ISO obliga a cada cliente a decidir por su
                    # cuenta cuando algo es viejo — y a discrepar entre si.
                    {**item.to_json(), "stale": item.is_stale(now=ahora)}
                    for item in items
                ],
            },
        )

    async def _confirm_memory(self, item_id: str) -> Response:
        """Una persona responde por esto. Es el unico camino a `USER_CONFIRMED`."""
        memory = self._memory_store()
        if memory is None:
            return Response(
                404, error_to_json("memory_disabled", "This deployment remembers nothing")
            )
        item = await memory.approve(item_id, state=VerificationState.USER_CONFIRMED)
        return Response(200, item.to_json())

    async def _forget_memory(self, item_id: str) -> Response:
        memory = self._memory_store()
        if memory is None:
            return Response(
                404, error_to_json("memory_disabled", "This deployment remembers nothing")
            )
        forgotten = await memory.forget(item_id)
        if not forgotten:
            return Response(404, error_to_json("not_found", f"No memory {item_id}"))
        return Response(200, {"id": item_id, "forgotten": True})

    def _memory_store(self) -> SqliteProjectMemory | None:
        return self.registry.orchestrator.settings.memory

    def _revise_goal(self, run_id: str, request: Request) -> Response:
        """Cambiar el encargo de un run vivo, diciendo sobre que revision se escribe.

        `base_revision` es obligatorio y no tiene valor por defecto. Uno implicito
        —«la ultima»— convertiria cada revision en un pisotón: dos personas mirando el
        mismo run se sobrescribirian sin enterarse, que es justo lo que el numero existe
        para impedir.
        """
        payload = request.json()
        objective = payload.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise ToolValidationError("objective must be a non-empty string")
        base = payload.get("base_revision")
        if isinstance(base, bool) or not isinstance(base, int):
            raise ToolValidationError("base_revision must be the revision you are revising")
        reason = payload.get("reason")
        revisado = self.registry.revise_goal(
            run_id,
            objective,
            base_revision=base,
            reason=reason if isinstance(reason, str) else "",
        )
        return Response(
            200,
            {
                "run_id": run_id,
                "goal": revisado.to_json(),
                # Escrito no es aplicado. El bucle recoge el cambio entre iteraciones y lo
                # anuncia con `goal.revised`; decir aqui que ya se esta trabajando en ello
                # seria comodo y falso.
                "applied": False,
            },
        )

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
