"""Ejecucion de herramientas: olas, admision y registro de lo ocurrido.

Las olas existen para que llamadas independientes no se serialicen sin
motivo, y la admision para que dos que tocan lo mismo no se pisen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace

from athena.agent_loop.sesion import SesionMixin
from athena.agent_loop.tipos import _RunData
from athena.budget import RuntimeBudget
from athena.cancellation import CancellationToken
from athena.errors import (
    AthenaRuntimeError,
    CancellationError,
    ProcessCancelledError,
)
from athena.events import (
    AgentEvent,
    EventName,
    RecoveryEvent,
    ToolEvent,
)
from athena.goals import announcement
from athena.models import (
    ModelMessage,
    ModelRole,
    ModelToolCall,
)
from athena.system1 import review_requested
from athena.tool_projection import model_view_of
from athena.tool_search import TOOL_SEARCH_NAME
from athena.tools import Tool, ToolResult
from athena.types import JSONObject, JSONValue
from athena.working_state import RecordedError
from athena.workspace import Workspace


class EjecucionMixin(SesionMixin):
    """Ejecucion de herramientas: olas, admision y registro de lo ocurrido."""

    async def _execute_calls(
        self,
        calls: tuple[ModelToolCall, ...],
        workspace: Workspace,
        cancellation: CancellationToken,
        data: _RunData,
        budget: RuntimeBudget,
    ) -> tuple[JSONObject | None, ...]:
        """Run a turn's calls, overlapping only the ones that said they may overlap.

        Three passes, and the split is what makes overlapping safe. Admission is
        sequential because it mutates the run — budget, seen ids, discovered paths — and
        two coroutines doing that concurrently would lose updates silently. Execution is
        the only part that overlaps. Recording is sequential again, and the transcript is
        assembled in the order the model asked, so it reads the same whatever order the
        work finished in.

        `ConcurrencyScheduler` decides the waves and is deliberately unpersuadable: a wave
        holds more than one call only when *both* tools declared themselves safe to run
        alongside another and their resources do not intersect. Anything unknown gets a
        wave of its own, which is the behaviour this loop had when it could not overlap
        anything at all.

        Results are held by position rather than by call id. Ids arrive from the model and
        can be empty or repeated — that is the first thing admission checks — so a map
        keyed by id would let a refused duplicate overwrite the answer of the call it
        duplicated.
        """
        payloads: list[JSONObject | None] = [None] * len(calls)
        admitted: list[tuple[int, ModelToolCall, Tool | None]] = []
        for index, call in enumerate(calls):
            cancellation.raise_if_cancelled()
            refusal = await self._admit(call, data, budget)
            if refusal is not None:
                payloads[index] = refusal
                continue
            try:
                tool: Tool | None = self.registry.get(call.name)
            except AthenaRuntimeError:
                # An unknown name is the executor's error to report, with its own code and
                # its own event. Claiming it here would duplicate that in a worse form.
                tool = None
            admitted.append((index, call, tool))

        for wave in self._waves(admitted):
            cancellation.raise_if_cancelled()
            data.session = replace(
                data.session,
                agent=replace(
                    data.session.agent,
                    active_tool_call_ids=tuple(call.call_id for _, call, _ in wave),
                ),
            )
            try:
                results = await asyncio.gather(
                    *(
                        self.executor.execute(
                            call,
                            session_id=data.session.session_id,
                            workspace=workspace,
                            cancellation=cancellation,
                            context_metadata={
                                "system1_review": {
                                    "objective": data.goal.current.text,
                                    "acceptance_criteria": list(self.config.acceptance_criteria),
                                    "state": data.working.to_json(),
                                    "output": data.latest_output,
                                    "mandatory_review": review_requested(
                                        data.goal.current.text, self.config.mandatory_review
                                    )
                                    or data.review_required
                                    or data.latest_output_truncated
                                    or bool(data.working.errors or data.working.remaining_work)
                                    or data.goal.pending is not None,
                                },
                                # Outside the evidence, which is sent to the broker.
                                "system1_review_declared": self.config.mandatory_review,
                            }
                            if self.system1 is not None
                            and self.system1.config.reviewer_gate
                            and call.name == "delegate_task"
                            else None,
                        )
                        for _, call, _ in wave
                    ),
                    return_exceptions=True,
                )
            finally:
                data.session = replace(
                    data.session,
                    agent=replace(data.session.agent, active_tool_call_ids=()),
                )
            for (index, call, _), outcome in zip(wave, results, strict=True):
                payloads[index] = await self._record(call, outcome, data)

        for call, payload in zip(calls, payloads, strict=True):
            if payload is not None:
                data.history.append(self._tool_message(call, payload))
        return tuple(payloads)

    def _waves(
        self, admitted: Sequence[tuple[int, ModelToolCall, Tool | None]]
    ) -> tuple[tuple[tuple[int, ModelToolCall, Tool | None], ...], ...]:
        """Group admitted calls into waves that may run together.

        A call whose tool could not be resolved runs alone, first: nothing is known about
        what it touches, and the cautious reading of "unknown" is "everything".
        """
        alone = [(entry,) for entry in admitted if entry[2] is None]
        schedulable = [entry for entry in admitted if entry[2] is not None]
        if not schedulable:
            return tuple(alone)
        by_id = {call.call_id: entry for entry in schedulable for _, call, _ in (entry,)}
        planned = self.scheduler.plan_calls(
            [(call.call_id, tool, call.arguments) for _, call, tool in schedulable if tool]
        )
        waves = [tuple(by_id[call_id] for call_id in batch.call_ids) for batch in planned]
        return tuple(alone) + tuple(waves)

    async def _admit(
        self, call: ModelToolCall, data: _RunData, budget: RuntimeBudget
    ) -> JSONObject | None:
        """Take a call into the turn, or refuse it. Returns the refusal payload."""
        if not call.call_id or call.call_id in data.seen_call_ids:
            error: JSONObject = {
                "ok": False,
                "error": {
                    "code": "tool_validation_error",
                    "message": "Tool call ID is empty or duplicated",
                },
                "call_id": call.call_id,
            }
            await self.event_bus.publish(
                ToolEvent(
                    EventName.TOOL_FAILED,
                    data.session.session_id,
                    {
                        "tool_name": call.name,
                        "error_code": "tool_validation_error",
                        "message": "Tool call ID is empty or duplicated",
                    },
                    call.call_id or None,
                )
            )
            return error
        data.seen_call_ids.add(call.call_id)
        budget.consume_tool_call()
        self._remember_paths(call, data)
        return None

    async def _take_revision(self, objective: str, data: _RunData) -> str:
        """Recoger un objetivo revisado, si alguien lo cambio desde la ultima vuelta."""
        anterior = data.goal.current
        nuevo = data.goal.take()
        if nuevo is None:
            return objective
        previo = next(
            (item for item in reversed(data.goal.history()) if item.revision < nuevo.revision),
            anterior,
        )
        # La evidencia obtenida bajo una revision no demuestra la siguiente. Heredarla
        # seria la forma mas barata de dar por bueno un trabajo que nadie pidio.
        data.last_verification = None
        data.working = replace(data.working, objective=nuevo.text).noting(
            decisions=(f"El objetivo cambio a la revision {nuevo.revision}.",),
        )
        data.history.append(ModelMessage(ModelRole.USER, announcement(previo, nuevo)))
        await self.event_bus.publish(
            AgentEvent(
                EventName.GOAL_REVISED,
                data.session.session_id,
                {
                    "revision": nuevo.revision,
                    "supersedes": previo.revision,
                    "reason": nuevo.reason,
                    "objective": nuevo.text,
                    # Que se anula, no solo que empieza: sin esto quien lo lea no sabe
                    # contra que se hizo todo lo anterior del run.
                    "superseded_objective": previo.text,
                },
            )
        )
        self._select_skills(nuevo.text, data)
        return nuevo.text

    async def _record(
        self, call: ModelToolCall, outcome: ToolResult | BaseException, data: _RunData
    ) -> JSONObject:
        """Turn one finished call into what the model will be told about it.

        Sequential by construction: it mutates the run's working state, and the whole
        point of separating it from execution is that two of these never interleave.
        """
        try:
            if isinstance(outcome, BaseException):
                raise outcome
            # Record what happened, never what was merely attempted: a refused write that
            # still showed up in files_modified would make the working state lie to
            # verification, to recovery and to whoever reads the session later.
            data.working = self._record_tool_use(data.working, call)
            data.review_required = (
                data.review_required or outcome.metadata.get("review_required") is True
            )
            output_text = str(outcome.output)
            # Truncated evidence blocks only a gate judging this output; it no longer marks
            # the whole run as sensitive, which disabled the goal checkpoint for any run
            # that had read a large file or run a verbose test suite.
            data.latest_output_truncated = len(output_text) > 2_000 or outcome.reference is not None
            data.latest_output = output_text[:2_000]
            if call.name == "delegate_task" and isinstance(outcome.output, dict):
                files = outcome.output.get("files_changed")
                commands = outcome.output.get("commands_run")
                if isinstance(files, list):
                    data.working = data.working.modifying(
                        files_modified=tuple(path for path in files if isinstance(path, str))
                    )
                if isinstance(commands, list):
                    for command in commands:
                        if isinstance(command, str):
                            data.working = data.working.ran(command)
            if outcome.reference is not None:
                data.references.append(outcome.reference)
            if call.name == TOOL_SEARCH_NAME:
                # Sobre el resultado canonico, no sobre la vista: revelar tools exige los
                # nombres estructurados, y leerlos del texto que se le enseña al modelo
                # seria derivar un hecho de su presentacion.
                self._reveal(outcome.output, data)
            # Al modelo se le cuenta la proyeccion cuando existe. Su contexto es caro y no
            # mejora por recibir el JSON entero de un listado de cien ficheros; lo que se
            # guarda y lo que se verifica siguen siendo el resultado canonico.
            view = model_view_of(outcome)
            told: JSONValue = outcome.output if view is None else view.text
            return {
                "ok": True,
                "call_id": outcome.call_id,
                "output": told,
                "truncated": view is not None and view.truncated,
                "reference_uri": outcome.reference.uri if outcome.reference else None,
            }
        except (CancellationError, ProcessCancelledError):
            # Being stopped is not a tool failure, so it does not go to the recovery
            # policy and does not become a recorded error against the task.
            raise
        except AthenaRuntimeError as exc:
            directive = self.recovery.decide(exc)
            data.working = data.working.failing(
                RecordedError(exc.code, exc.message, directive.action.value)
            )
            await self.event_bus.publish(
                RecoveryEvent(
                    EventName.RECOVERY_ACTION,
                    data.session.session_id,
                    {
                        "error_code": exc.code,
                        "action": directive.action.value,
                        "reason": directive.reason,
                    },
                    call.call_id,
                )
            )
            if directive.ends_run:
                # `ends_run` estaba declarado y no lo miraba nadie: ABORT se documenta como
                # «abandona la accion y falla el run», y aqui se convertia en un `ok: false`
                # mas que el modelo leia y seguia. Se vio en un run real que recibio ocho
                # directivas de abandono seguidas y murio treinta minutos despues por
                # presupuesto, escribiendo entre medias ficheros que nadie pidio. La
                # excepcion sube al manejador del run, que ya sabe cerrarlo con su codigo.
                raise
            return {
                "ok": False,
                "call_id": call.call_id,
                "recovery": directive.action.value,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            }
