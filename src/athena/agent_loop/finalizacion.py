"""Cierre del trabajo: progreso, verificacion, reparacion y rescate.

Un run no se declara completo porque el modelo lo diga: se verifica, y si
la verificacion falla se abre un ciclo de reparacion antes de rendirse.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from athena.agent_loop.ejecucion import EjecucionMixin
from athena.agent_loop.tipos import AgentRunResult, AgentRunStatus, _RunData
from athena.async_utils import await_cancellable
from athena.budget import RuntimeBudget
from athena.cancellation import CancellationToken
from athena.diagnosis import InconclusiveReason, diagnose_result, inconclusive_reason
from athena.errors import (
    AthenaRuntimeError,
    CancellationError,
    NoProgressError,
    ProcessCancelledError,
    VerificationFailure,
    VerificationInconclusive,
)
from athena.events import (
    AgentEvent,
    EventName,
    ModelEvent,
    RecoveryEvent,
    VerificationEvent,
)
from athena.hooks import (
    HookEvent,
)
from athena.models import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelToolCall,
)
from athena.progress import ProgressVerdict, turn_signature
from athena.recovery import RecoveryAction
from athena.state import (
    AgentStatus,
)
from athena.system1 import deterministic_ready, explicit_review, verification_input
from athena.types import JSONObject
from athena.verification import (
    VerificationResult,
    VerificationStatus,
    evidence_digest,
)
from athena.working_state import RecordedError
from athena.workspace import Workspace


class FinalizacionMixin(EjecucionMixin):
    """Cierre del trabajo: progreso, verificacion, reparacion y rescate."""

    async def _complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        data: _RunData,
        budget: RuntimeBudget,
    ) -> ModelResponse:
        request_id = str(uuid4())
        # Antes de gastar nada. Una petición con herramientas a un proveedor que no las
        # admite no falla al enviarse: falla más adelante, como una respuesta rara, lejos
        # de su causa. Preguntarlo aquí cuesta una comparación y convierte ese fallo en un
        # error con nombre.
        await self._require_capabilities(request, data, request_id)
        data.session = replace(
            data.session,
            agent=replace(data.session.agent, active_model_request_id=request_id),
        )
        attempt = 0
        while True:
            budget.consume_model_call()
            await self.event_bus.publish(
                ModelEvent(
                    EventName.MODEL_STARTED,
                    data.session.session_id,
                    {"attempt": attempt + 1},
                    request_id,
                )
            )
            try:
                response = await await_cancellable(
                    self.provider.complete(request, cancellation),
                    cancellation,
                )
            except AthenaRuntimeError as exc:
                directive = self.recovery.decide(exc)
                retrying = directive.retries and attempt < directive.max_attempts
                await self.event_bus.publish(
                    ModelEvent(
                        EventName.MODEL_FAILED,
                        data.session.session_id,
                        {"error_code": exc.code, "retrying": retrying},
                        request_id,
                    )
                )
                await self.event_bus.publish(
                    RecoveryEvent(
                        EventName.RECOVERY_STARTED,
                        data.session.session_id,
                        {
                            "error_code": exc.code,
                            "action": directive.action.value,
                            "reason": directive.reason,
                        },
                        request_id,
                    )
                )
                data.working = data.working.failing(
                    RecordedError(exc.code, exc.message, directive.action.value)
                )
                if not retrying:
                    if directive.retries:
                        await self.event_bus.publish(
                            RecoveryEvent(
                                EventName.RECOVERY_EXHAUSTED,
                                data.session.session_id,
                                {"error_code": exc.code, "attempts": attempt + 1},
                                request_id,
                            )
                        )
                    raise
                await self.event_bus.publish(
                    RecoveryEvent(
                        EventName.RECOVERY_ACTION,
                        data.session.session_id,
                        {"action": directive.action.value, "attempt": attempt + 1},
                        request_id,
                    )
                )
                if directive.action is RecoveryAction.COMPACT_CONTEXT:
                    request = self._compact(request)
                    data.compactions += 1
                if directive.backoff_seconds:
                    await await_cancellable(
                        asyncio.sleep(directive.backoff_seconds * (2**attempt)),
                        cancellation,
                    )
                attempt += 1
                continue
            data.session = replace(
                data.session,
                agent=replace(data.session.agent, active_model_request_id=None),
            )
            await self.event_bus.publish(
                ModelEvent(
                    EventName.MODEL_COMPLETED,
                    data.session.session_id,
                    {
                        "finish_reason": response.finish_reason,
                        "tool_call_count": len(response.tool_calls),
                        # El proveedor los cuenta y hasta aquí se perdían: el adaptador
                        # los ponía en la respuesta y el evento no los llevaba, así que
                        # toda medición de tokens salía a cero pareciendo un dato.
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        # El modelo que contestó de verdad, que con un router por delante
                        # no tiene por qué ser el que se pidió.
                        "model": response.model,
                    },
                    request_id,
                )
            )
            return response

    async def _check_progress(
        self,
        calls: tuple[ModelToolCall, ...],
        payloads: tuple[JSONObject | None, ...],
        data: _RunData,
    ) -> NoProgressError | None:
        """Mirar si el turno que acaba de pasar es el mismo que el anterior.

        Se llama despues de ejecutar y antes del checkpoint, porque la firma necesita los
        resultados: dos turnos que piden lo mismo y reciben cosas distintas si avanzan.

        El aviso entra en el historial como un mensaje de usuario y no como una nota de
        estado. Una nota se resume junto al resto del estado de trabajo y compite con el;
        lo que hace falta aqui es que el modelo lea, en el turno siguiente y sin
        intermediarios, que lo que acaba de hacer ya lo habia hecho.
        """
        verdict = data.progress.observe(
            turn_signature([(call.name, call.arguments) for call in calls], payloads)
        )
        if verdict is ProgressVerdict.PROGRESSING:
            return None
        repeated = sorted({call.name for call in calls})
        await self.event_bus.publish(
            RecoveryEvent(
                EventName.RECOVERY_ACTION,
                data.session.session_id,
                {
                    "action": "no_progress",
                    "verdict": verdict.value,
                    "repeats": data.progress.repeats,
                    "tools": repeated,
                    "reason": "The same tool calls returned the same results again.",
                },
            )
        )
        if verdict is ProgressVerdict.STUCK:
            return NoProgressError(
                "The run repeated the same tool calls with the same results "
                f"{data.progress.repeats + 1} times without progressing",
                details={"tools": repeated, "repeats": data.progress.repeats},
            )
        data.working = data.working.noting(
            decisions=("The last turns repeated the same tool calls and got the same results.",),
            remaining_work=("Change approach: the repeated reads are not producing anything new.",),
        )
        data.history.append(
            ModelMessage(
                ModelRole.USER,
                "Stop. You have just made the same tool calls as the previous turn and "
                f"received the same results ({', '.join(repeated)}). Re-reading them will "
                "not tell you anything new. Either act on what you already know — write "
                "or edit the files the objective asks for — or give your final answer. "
                "If you repeat this turn again the run will be abandoned.",
            )
        )
        return None

    async def _attempt_completion(
        self,
        response: ModelResponse,
        data: _RunData,
        workspace: Workspace,
        cancellation: CancellationToken,
        budget: RuntimeBudget,
    ) -> AgentRunResult | None:
        """Verify the work. Returns None when a repair cycle should run instead."""
        if response.finish_reason not in ("stop", "done") or not response.content.strip():
            raise VerificationFailure("Model response did not satisfy terminal conditions")
        if self.config.require_workspace_change and not data.working.files_modified:
            data.working = data.working.noting(
                decisions=(
                    "A final response was refused because the objective requires a workspace "
                    "change and no file has been modified.",
                ),
                remaining_work=(
                    "Use an offered workspace mutation tool and confirm its successful result.",
                ),
            )
            data.history.append(
                ModelMessage(
                    ModelRole.USER,
                    "The objective explicitly requires creating or modifying a file, but no "
                    "workspace file has changed. Do not return a final answer yet. Use one of "
                    "Athena's offered write or edit tools with the required content, wait for "
                    "its result, and only then finish.",
                )
            )
            await self.event_bus.publish(
                RecoveryEvent(
                    EventName.RECOVERY_ACTION,
                    data.session.session_id,
                    {
                        "action": "require_workspace_change",
                        "reason": "No successful file mutation was observed.",
                    },
                )
            )
            return None
        verification, razon = await self._run_verification(response, data, workspace, cancellation)
        if verification.permits_completion:
            verdict = await self._system1_goal(response, verification, data, cancellation)
            if data.goal.pending is not None:
                return None
            if verdict is False:
                return await self._semantic_repair(data)
            return await self._complete_run(response, data, workspace, budget, verification)
        if verification.status is VerificationStatus.INCONCLUSIVE:
            # Un run cuyos checks no pudieron ejecutarse no ha fallado la verificacion: ha
            # fallado en verificar. Contar lo segundo como lo primero le echa la culpa al
            # cambio de una maquina rota, y quien lo lea corregira lo que no estaba mal.
            raise VerificationInconclusive(
                verification.summary,
                details={"reason": (razon or InconclusiveReason.AMBIGUOUS_RESULT).value},
            )
        return await self._start_repair_cycle(data, verification)

    async def _salvage(
        self,
        data: _RunData,
        workspace: Workspace,
        cancellation: CancellationToken,
        budget: RuntimeBudget,
    ) -> AgentRunResult | None:
        """Un ultimo intento de comprobar si el trabajo ya estaba hecho. Uno, no un ciclo.

        Se llama cuando el run se va a abandonar: por estancamiento o por quedarse sin
        iteraciones. Las dos son la misma situacion vista de cerca —el bucle se acaba y en
        el disco puede haber un trabajo terminado que nadie ha mirado—. Corre la misma
        verificacion que el camino normal —el mismo `_run_verification`, no una version
        parecida— y solo termina el run si esa evidencia da para terminarlo.

        Si no da, devuelve `None` y el run se abandona como estaba previsto: esto no es un
        ciclo de reparacion ni una segunda oportunidad para el modelo, es leer una vez lo
        que ya hay en el disco antes de tirarlo.
        """
        if not data.working.files_modified:
            # Nada que comprobar: un run que no cambio un fichero no ha hecho el trabajo,
            # y gastar una suite de tests para confirmarlo es gastar por gastar.
            return None
        # El modelo nunca dio una respuesta final —estaba dando vueltas—, asi que no se
        # inventa una: se dice lo que paso. Poner aqui una frase en su nombre seria
        # atribuirle una conclusion que no llego a sacar.
        aviso = ModelResponse(
            "El modelo se detuvo por repetirse sin avanzar. Lo que dejó escrito "
            "en el proyecto sí supera la verificación de este trabajo; la evidencia va "
            "adjunta.",
            "athena",
            "stop",
        )
        try:
            verification, _ = await self._run_verification(aviso, data, workspace, cancellation)
        except (CancellationError, ProcessCancelledError):
            # Que a alguien se le acabe la paciencia a mitad del rescate no es «el rescate
            # no pudo comprobar nada»: es que pararon el run. La regla 12 dice que la
            # cancelacion se propaga entera, y tragarsela aqui la convertiria en un
            # `budget_exceeded` que le echa la culpa al reloj de una decision de alguien.
            raise
        except AthenaRuntimeError:
            # Que la comprobacion de rescate no se pueda ejecutar no cambia el diagnostico
            # original: el run seguia abandonado y se cierra por eso, no por esto.
            return None
        if not verification.permits_completion:
            return None
        verdict = await self._system1_goal(aviso, verification, data, cancellation)
        if verdict is False or data.goal.pending is not None:
            return None
        return await self._complete_run(aviso, data, workspace, budget, verification)

    async def _system1_goal(
        self,
        response: ModelResponse,
        verification: VerificationResult,
        data: _RunData,
        cancellation: CancellationToken,
    ) -> bool | None:
        if self.system1 is None or not self.system1.config.goal_completion:
            return None
        # A failed required check can never be overridden, even if attributed to baseline.
        if any(item.metadata.get("passed") is False for item in verification.evidence):
            return None
        verdict = await self.system1.completed(
            {
                "objective": data.goal.current.text,
                "acceptance_criteria": list(self.config.acceptance_criteria),
                "state": data.working.to_json(),
                "output": response.content,
                "checks": verification_input(verification),
                "pending": list(data.working.remaining_work),
            },
            cancellation,
            session_id=data.session.session_id,
        )
        # Evidence collected for an earlier objective cannot complete a revised one.
        return None if data.goal.pending is not None else verdict

    async def _semantic_repair(self, data: _RunData) -> None:
        if data.repair_cycles >= self.config.max_repair_cycles:
            raise VerificationFailure("System-1 found an unmet objective or acceptance criterion")
        data.repair_cycles += 1
        data.history.append(
            ModelMessage(
                ModelRole.USER,
                "The project checks passed, but the semantic completion checkpoint found that "
                "the objective or an acceptance criterion is still incomplete. Compare EVERY "
                "part of the objective against the actual artifacts, including any requested "
                "regression test. Complete the missing work and provide evidence.",
            )
        )
        await self.event_bus.publish(
            RecoveryEvent(
                EventName.RECOVERY_STARTED,
                data.session.session_id,
                {"reason": "system1_incomplete", "repair_cycle": data.repair_cycles},
            )
        )

    async def _system1_checkpoint(
        self,
        response: ModelResponse,
        payloads: tuple[JSONObject | None, ...],
        data: _RunData,
        workspace: Workspace,
        cancellation: CancellationToken,
        budget: RuntimeBudget,
    ) -> AgentRunResult | None:
        if self.system1 is None or not self.system1.config.goal_completion:
            return None
        # A block with changed files and a successful check command has evidence worth
        # examining. Reads and micro-actions never trigger another verification suite.
        if not data.working.files_modified or data.goal.pending is not None:
            return None
        if (
            self.config.mandatory_review
            or data.review_required
            or explicit_review(data.goal.current.text)
        ):
            return None
        if not any(call.name == "bash" for call in response.tool_calls):
            return None
        if any(payload is None or payload.get("ok") is not True for payload in payloads):
            return None
        checkpoint = ModelResponse(
            "Athena comprobó los artefactos y el objetivo tras el bloque de trabajo.",
            "athena",
            "stop",
        )
        old_session, old_working = data.session, data.working
        try:
            verification, _ = await self._run_verification(
                checkpoint, data, workspace, cancellation
            )
        except (CancellationError, ProcessCancelledError):
            raise
        except AthenaRuntimeError:
            data.session, data.working = old_session, old_working
            return None
        verdict = (
            await self._system1_goal(checkpoint, verification, data, cancellation)
            if deterministic_ready(verification)
            else None
        )
        if verdict is True and not data.working.remaining_work and data.goal.pending is None:
            await self.event_bus.publish(
                AgentEvent(
                    EventName.SYSTEM1_COMPLETION,
                    data.session.session_id,
                    {"source": "checkpoint", "auto_completed": True},
                )
            )
            return await self._complete_run(checkpoint, data, workspace, budget, verification)
        # An observation must not manufacture a terminal model response in a live run.
        data.session, data.working = old_session, old_working
        return None

    async def _run_verification(
        self,
        response: ModelResponse,
        data: _RunData,
        workspace: Workspace,
        cancellation: CancellationToken,
    ) -> tuple[VerificationResult, InconclusiveReason | None]:
        """Reunir la evidencia y contarla. El unico sitio donde se verifica.

        Lo usan los dos caminos que pueden acabar un run: el modelo que dice haber
        terminado y el run que se abandona por estancamiento. Estaba escrito una vez y en
        linea; sacarlo aqui es lo que impide que el segundo camino verifique de una forma
        ligeramente distinta y acabe respondiendo otra cosa sobre el mismo trabajo.
        """
        data.session = replace(
            data.session,
            agent=replace(data.session.agent, status=AgentStatus.VERIFYING),
            attributes={
                **data.session.attributes,
                "final_response": response.content,
                "finish_reason": response.finish_reason,
                # Lo que el run hizo, no solo lo ultimo que dijo. Una politica de
                # verificacion a la que solo se le cuenta la frase final tiene que
                # deducir el trabajo del texto del modelo, que es justo lo unico que
                # no es evidencia.
                "files_modified": list(data.working.files_modified),
                "commands_run": list(data.working.commands_run),
            },
            updated_at=datetime.now(UTC),
        )
        await self.event_bus.publish(
            VerificationEvent(EventName.VERIFICATION_STARTED, data.session.session_id)
        )
        await self._hook(
            HookEvent.PRE_VERIFY,
            data.session.session_id,
            {"files_modified": list(data.working.files_modified)},
        )
        verification = await await_cancellable(
            self.verification.verify(data.session, workspace, cancellation),
            cancellation,
        )
        await self._hook_quietly(
            HookEvent.POST_VERIFY,
            data.session.session_id,
            {
                "status": verification.status.value,
                "summary": verification.summary,
                "evidence_count": len(verification.evidence),
            },
        )
        data.last_verification = verification
        data.working = data.working.verified(
            {
                "status": verification.status.value,
                "summary": verification.summary,
                "evidence_count": len(verification.evidence),
            }
        )
        # Por que no se pudo concluir, no solo que no se concluyo. «Inconclusive» a secas
        # se lee como un problema de configuracion, y casi nunca lo es: un servicio caido,
        # un entorno a medio instalar y un proyecto sin checks dejan el mismo hueco en la
        # evidencia y piden cosas distintas de quien lo lea.
        razon = inconclusive_reason(diagnose_result(verification))
        await self.event_bus.publish(
            VerificationEvent(
                EventName.VERIFICATION_COMPLETED,
                data.session.session_id,
                {
                    "status": verification.status.value,
                    "evidence_count": len(verification.evidence),
                    "inconclusive_reason": None if razon is None else razon.value,
                },
            )
        )
        return verification, razon

    async def _start_repair_cycle(
        self, data: _RunData, verification: VerificationResult
    ) -> AgentRunResult | None:
        session_id = data.session.session_id
        directive = self.recovery.decide(VerificationFailure(verification.summary))
        await self.event_bus.publish(
            RecoveryEvent(
                EventName.RECOVERY_STARTED,
                session_id,
                {"error_code": "verification_failure", "action": directive.action.value},
            )
        )
        if data.repair_cycles >= self.config.max_repair_cycles:
            await self.event_bus.publish(
                RecoveryEvent(
                    EventName.RECOVERY_EXHAUSTED,
                    session_id,
                    {
                        "error_code": "verification_failure",
                        "repair_cycles": data.repair_cycles,
                    },
                )
            )
            raise VerificationFailure(
                f"Verification still failing after {data.repair_cycles} repair cycle(s): "
                f"{verification.summary}"
            )
        # Read the failure before asking anyone to fix it. A wall of pytest output is a
        # lot to hand a small model; telling it which *kind* of problem this is turns the
        # next cycle from a guess into a direction.
        diagnosis = diagnose_result(verification)
        if not diagnosis.is_worth_repairing:
            # A missing package or a full disk will not be fixed by editing code, and
            # spending a cycle letting the model try is how a run burns its budget looking
            # busy. Stop and say what is actually wrong.
            #
            # Y decir cual de las dos cosas es: un paquete que falta deja el mismo hueco
            # que un proyecto sin checks —no se probo nada—, mientras que un test que
            # afirma lo contrario de lo que se pidio si es un fallo. Llamarlos igual
            # obligaria a leerse la salida entera para saber a quien culpar.
            razon = inconclusive_reason(diagnosis)
            codigo = "verification_failure" if razon is None else VerificationInconclusive.code
            await self.event_bus.publish(
                RecoveryEvent(
                    EventName.RECOVERY_EXHAUSTED,
                    session_id,
                    {
                        "error_code": codigo,
                        "repair_cycles": data.repair_cycles,
                        "diagnosis": diagnosis.kind.value,
                        "inconclusive_reason": None if razon is None else razon.value,
                    },
                )
            )
            mensaje = f"{diagnosis.summary} No repair cycle can address this: {diagnosis.guidance}"
            if razon is not None:
                raise VerificationInconclusive(mensaje, details={"reason": razon.value})
            raise VerificationFailure(mensaje)

        data.repair_cycles += 1
        data.working = data.working.noting(
            decisions=(f"Repair cycle {data.repair_cycles}: {verification.summary}",),
            remaining_work=("Make the failing verification checks pass.",),
        )
        await self.event_bus.publish(
            RecoveryEvent(
                EventName.RECOVERY_ACTION,
                session_id,
                {
                    "action": RecoveryAction.RETURN_EVIDENCE.value,
                    "repair_cycle": data.repair_cycles,
                    "diagnosis": diagnosis.kind.value,
                },
            )
        )
        data.history.append(
            ModelMessage(
                ModelRole.USER,
                "Your change did not pass verification. Do not weaken, skip or delete "
                "any check. Fix the underlying problem and finish again.\n\n"
                + diagnosis.render()
                + "\n\n"
                + evidence_digest(verification),
            )
        )
        return None

    async def _complete_run(
        self,
        response: ModelResponse,
        data: _RunData,
        workspace: Workspace,
        budget: RuntimeBudget,
        verification: VerificationResult,
    ) -> AgentRunResult:
        data.session = self._with_budget(data.session, budget, AgentStatus.COMPLETED)
        await self._persist(
            data,
            workspace,
            AgentStatus.COMPLETED,
            "completed",
            {"verification": verification.status.value},
        )
        data.session = replace(
            data.session,
            attributes={
                **data.session.attributes,
                "working_state": data.working.to_json(),
            },
        )
        await self.event_bus.publish(
            AgentEvent(
                EventName.AGENT_COMPLETED,
                data.session.session_id,
                {
                    "iterations": budget.usage.iterations,
                    "tool_calls": budget.usage.tool_calls,
                    "repair_cycles": data.repair_cycles,
                    "verification": verification.summary,
                },
            )
        )
        await self._finish(data, "completed")
        return AgentRunResult(
            AgentRunStatus.COMPLETED,
            data.session,
            answer=response.content,
            tool_call_ids=tuple(data.seen_call_ids),
            verification=verification,
            working_state=data.working,
        )
