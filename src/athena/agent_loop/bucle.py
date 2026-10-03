"""El bucle propiamente dicho: `run`, `resume` y una vuelta completa."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from athena.agent_loop.finalizacion import FinalizacionMixin
from athena.agent_loop.tipos import AgentRunResult, AgentRunStatus, _RunData
from athena.budget import BudgetLimits, RuntimeBudget
from athena.cancellation import CancellationToken
from athena.errors import (
    AthenaRuntimeError,
    BudgetExceededError,
    CancellationError,
    FatalRuntimeError,
    ProcessCancelledError,
    ProcessTimeoutError,
)
from athena.events import (
    AgentEvent,
    EventName,
)
from athena.goals import GoalBoard
from athena.hooks import (
    HookEvent,
)
from athena.models import (
    ModelMessage,
    ModelRole,
)
from athena.session_store import (
    SessionRecord,
    SessionStoreError,
)
from athena.skills import render_skills
from athena.state import (
    AgentState,
    AgentStatus,
    BudgetState,
    SessionState,
    classify_outcome,
)
from athena.working_state import WorkingState
from athena.workspace import Workspace


class AgentLoop(FinalizacionMixin):
    """Ejecuta un objetivo hasta completarlo, verificarlo o agotar su presupuesto."""

    async def resume(
        self,
        session_id: str,
        workspace: Workspace,
        cancellation: CancellationToken,
    ) -> AgentRunResult:
        """Continue an interrupted session from stored working memory alone.

        No transcript is replayed. Everything the run needs was persisted as structured
        state, which is the point of keeping it out of the chat in the first place.
        """
        if self.session_store is None:
            raise SessionStoreError("Resuming requires a session store")
        record = await self.session_store.load(session_id)
        if record is None:
            raise SessionStoreError(f"Unknown session: {session_id}")
        if not record.resumable:
            raise SessionStoreError(
                f"Session {session_id} is {record.status.value}, not recovery_pending"
            )
        await self.event_bus.publish(
            AgentEvent(
                EventName.SESSION_RESUMED,
                session_id,
                {
                    "objective": record.working_memory.objective,
                    "degraded": record.degraded,
                    "files_modified": list(record.working_memory.files_modified),
                },
            )
        )
        return await self.run(
            record.working_memory.objective, workspace, cancellation, resume_from=record
        )

    async def run(
        self,
        objective: str,
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        resume_from: SessionRecord | None = None,
        session_id: str | None = None,
        goal: GoalBoard | None = None,
    ) -> AgentRunResult:
        # An external caller may name the run before it starts. Without this a service
        # cannot address a run until the loop has already emitted events about it.
        if resume_from is not None:
            session_id = resume_from.session_id
        elif session_id is None:
            session_id = str(uuid4())
        initial_agent = AgentState(
            AgentStatus.RUNNING,
            budget=BudgetState(max_steps=self.config.max_iterations),
        )
        working = (
            resume_from.working_memory
            if resume_from is not None
            else WorkingState(objective=objective)
        )
        data = _RunData(
            SessionState(session_id, workspace.workspace_id, initial_agent),
            working,
            # Quien encargo el trabajo puede seguir hablando mientras se hace. Sin tablero
            # el objetivo es el que llego y no cambia, que es lo de siempre.
            goal=goal if goal is not None else GoalBoard(objective),
            # The durable session does not retain a complete executor output to review.
            review_required=resume_from is not None,
        )
        if resume_from is not None:
            data.references.extend(resume_from.tool_references)
            data.checkpoints.extend(resume_from.checkpoints)
        await self.event_bus.publish(
            AgentEvent(
                EventName.AGENT_STARTED,
                session_id,
                {"objective": objective, "resumed": resume_from is not None},
            )
        )
        await self._persist(data, workspace, AgentStatus.RUNNING, "started")
        self._select_skills(objective, data)
        await self._hook(
            HookEvent.SESSION_START,
            session_id,
            {
                "objective": objective,
                "resumed": resume_from is not None,
                "skills": [selection.skill.name for selection in data.skills],
            },
        )
        try:
            return await asyncio.wait_for(
                self._iterate(objective, workspace, cancellation, data),
                timeout=self.config.session_timeout_seconds,
            )
        except TimeoutError:
            timeout = ProcessTimeoutError("Agent session timed out")
            failed = self._set_status(data.session, AgentStatus.FAILED, timeout.code)
            await self._persist(
                data, workspace, AgentStatus.FAILED, "failed", {"error_code": timeout.code}
            )
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    {"error_code": timeout.code, "message": timeout.message},
                )
            )
            await self._finish(data, "failed", timeout.code, timeout.message)
            return AgentRunResult(
                AgentRunStatus.FAILED,
                failed,
                error=timeout,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except (CancellationError, ProcessCancelledError) as exc:
            # Classified rather than assumed: a cancellation raised because a deadline
            # passed is a timeout, and telling the person their work was abandoned when a
            # limit they set was reached is the wrong story.
            outcome = classify_outcome(exc)
            cancelled = self._set_status(data.session, AgentStatus.CANCELLED, exc.code)
            await self._persist(
                data, workspace, AgentStatus.CANCELLED, "cancelled", {"error_code": exc.code}
            )
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_CANCELLED,
                    session_id,
                    {
                        "error_code": exc.code,
                        "message": exc.message,
                        "outcome": outcome.value,
                    },
                )
            )
            await self._finish(data, "cancelled", exc.code, exc.message)
            return AgentRunResult(
                AgentRunStatus.CANCELLED,
                cancelled,
                error=exc,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except AthenaRuntimeError as exc:
            failed = self._set_status(data.session, AgentStatus.FAILED, exc.code)
            await self._persist(
                data, workspace, AgentStatus.FAILED, "failed", {"error_code": exc.code}
            )
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    # Los detalles tipados viajan con el fallo. Sin ellos la unica pista
                    # de por que un run no se pudo verificar vive dentro de una frase, y
                    # nada que cuente puede leer una frase.
                    {"error_code": exc.code, "message": exc.message, **exc.details},
                )
            )
            await self._finish(data, "failed", exc.code, exc.message)
            return AgentRunResult(
                AgentRunStatus.FAILED,
                failed,
                error=exc,
                tool_call_ids=tuple(data.seen_call_ids),
            )
        except Exception as exc:
            # Keep the original: an unclassified failure is hard enough to diagnose
            # without the runtime throwing away what actually went wrong.
            fatal = FatalRuntimeError(
                f"Unexpected runtime failure: {type(exc).__name__}: {exc}",
                details={"exception_type": type(exc).__name__, "detail": str(exc)},
            )
            fatal.__cause__ = exc
            failed = self._set_status(data.session, AgentStatus.FAILED, fatal.code)
            await self._persist(
                data, workspace, AgentStatus.FAILED, "failed", {"error_code": fatal.code}
            )
            await self.event_bus.publish(
                AgentEvent(
                    EventName.AGENT_FAILED,
                    session_id,
                    {"error_code": fatal.code, "message": fatal.message},
                )
            )
            await self._finish(data, "failed", fatal.code, fatal.message)
            return AgentRunResult(
                AgentRunStatus.FAILED,
                failed,
                error=fatal,
                tool_call_ids=tuple(data.seen_call_ids),
            )

    async def _iterate(
        self,
        objective: str,
        workspace: Workspace,
        cancellation: CancellationToken,
        data: _RunData,
    ) -> AgentRunResult:
        budget = RuntimeBudget(
            BudgetLimits(
                max_iterations=self.config.max_iterations,
                max_model_calls=self.config.max_iterations * (self.config.max_model_retries + 1),
                max_tool_calls=self.config.max_tool_calls,
            )
        )
        await self._capture_baseline(workspace, cancellation)
        for iteration in range(1, self.config.max_iterations + 1):
            cancellation.raise_if_cancelled()
            # El encargo se recoge aqui y solo aqui. Un objetivo que cambiase con una tool
            # a medias dejaria al modelo con un resultado pedido por un encargo y una
            # pregunta hecha por otro.
            objective = await self._take_revision(objective, data)
            budget.consume_iteration()
            data.session = self._with_budget(data.session, budget, AgentStatus.RUNNING)
            history = self._select_context(data)
            if data.pending_compaction is not None:
                report = data.pending_compaction
                data.pending_compaction = None
                await self.event_bus.publish(
                    AgentEvent(
                        EventName.CONTEXT_COMPACTED,
                        data.session.session_id,
                        {
                            "messages_before": report.messages_before,
                            "messages_after": report.messages_after,
                            "chars_before": report.chars_before,
                            "chars_after": report.chars_after,
                            "reasons": list(report.reasons),
                        },
                    )
                )
            request = await self.context_builder.build_request(
                objective=objective,
                history=history,
                important_state={
                    "iteration": iteration,
                    "tool_calls": budget.usage.tool_calls,
                    "repair_cycle": data.repair_cycles,
                    "working_state": data.working.summary(),
                    **(
                        {"acceptance_criteria": list(self.config.acceptance_criteria)}
                        if self.config.acceptance_criteria
                        else {}
                    ),
                    "skills": render_skills(data.skills),
                    "deferred_tools_available": len(self.registry.deferred_names()),
                },
                tool_definitions=self.registry.definitions(data.revealed_tools),
                cancellation=cancellation,
                discovered_paths=tuple(sorted(data.discovered_paths)),
                session_id=data.session.session_id,
            )
            if self.config.model:
                request = replace(request, model=self.config.model)
            if self.config.require_workspace_change and not data.working.files_modified:
                request = replace(
                    request,
                    options={**dict(request.options), "tool_choice": "required"},
                )
            response = await self._complete(request, cancellation, data, budget)
            data.history.append(
                ModelMessage(
                    ModelRole.ASSISTANT,
                    response.content,
                    tool_calls=response.tool_calls,
                )
            )
            if response.tool_calls:
                payloads = await self._execute_calls(
                    response.tool_calls,
                    workspace,
                    cancellation,
                    data,
                    budget,
                )
                completed = await self._system1_checkpoint(
                    response, payloads, data, workspace, cancellation, budget
                )
                if completed is not None:
                    return completed
                estancado = await self._check_progress(response.tool_calls, payloads, data)
                if estancado is not None:
                    # Antes de tirar el run, mirar si el trabajo ya estaba hecho. Un modelo
                    # que se atasca repitiendo `pytest` despues de haber arreglado el codigo
                    # ha terminado el encargo: lo que no sabe es decirlo. Medido: en un run
                    # real `nemotron-3.5-lightning:30b` dejo los tests en verde y se abandono
                    # sin verificar, asi que el trabajo bueno se reporto como fallo.
                    #
                    # No es regalarle el final al modelo. La regla 10 dice que la
                    # finalizacion no depende de que el LLM diga «done», sino de que haya
                    # evidencia; aqui no hay «done» y si se exige la evidencia entera.
                    rescatado = await self._salvage(data, workspace, cancellation, budget)
                    if rescatado is not None:
                        return rescatado
                    raise estancado
                # Checkpoint here: the agent has just changed the world, and a crash
                # before the next turn must not lose the record of what it changed.
                await self._persist(
                    data,
                    workspace,
                    AgentStatus.RUNNING,
                    "tool_calls",
                    {
                        "iteration": iteration,
                        "tool_calls": [call.name for call in response.tool_calls],
                    },
                )
                continue
            completed = await self._attempt_completion(
                response, data, workspace, cancellation, budget
            )
            if completed is not None:
                return completed
            # Only checkpoint an ongoing run: a terminal state was already written.
            await self._persist(
                data,
                workspace,
                AgentStatus.RUNNING,
                "iteration",
                {"iteration": iteration, "repair_cycles": data.repair_cycles},
            )
        # Quedarse sin iteraciones es otra forma de abandonar, y tira el trabajo igual: un
        # modelo lento puede haber arreglado el codigo en la iteracion 9 y gastar las tres
        # que quedan mirandolo. Medido: `nemotron-3.5-lightning:30b` dejo los tests en
        # verde y el run se reporto como fallo sin haber verificado una sola vez.
        #
        # La misma comprobacion unica que en el estancamiento, y con la misma exigencia:
        # solo termina si la evidencia da para terminar.
        rescatado = await self._salvage(data, workspace, cancellation, budget)
        if rescatado is not None:
            return rescatado
        raise BudgetExceededError("Maximum agent iterations reached without completion")
