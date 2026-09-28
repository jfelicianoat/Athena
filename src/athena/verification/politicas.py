"""Las politicas de verificacion que deciden si un run puede darse por completo.

La de comandos ejecuta lo planificado, compara contra la linea base y solo
atribuye un fallo al trabajo si antes pasaba.
"""

from __future__ import annotations

import time

from athena.cancellation import CancellationToken
from athena.errors import AthenaRuntimeError, ProcessTimeoutError
from athena.events import EventBus, EventName, VerificationEvent
from athena.process_tools import run_process
from athena.state import SessionState
from athena.types import JSONObject
from athena.verification.artefactos import _declared_paths
from athena.verification.autorizacion import CheckAuthorizer
from athena.verification.contratos import (
    _CHECK_ENVIRONMENT,
    _DEFAULT_CHECK_TIMEOUT,
    _MAX_OUTPUT_TAIL,
    Baseline,
    CheckKind,
    CheckOutcome,
    VerificationCheck,
    VerificationEvidence,
    VerificationResult,
    VerificationStatus,
)
from athena.verification.integridad import (
    ChangeIntegrityPolicy,
    IntegrityFinding,
    without_preexisting,
)
from athena.verification.plan import VerificationPlanner
from athena.workspace import Workspace
from athena.workspace_access import WORKSPACE_ACCESS

#: La clase de evidencia que deja una verificacion que no pudo ejecutar el plan porque
#: nadie lo autorizo. El diagnostico la lee para no llamarla «no hay checks».
NOT_AUTHORIZED_EVIDENCE = "authorization"


class LoopCompletionVerificationPolicy:
    """Minimal proof that the loop reached a defined, tool-free terminal response."""

    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        del workspace
        cancellation.raise_if_cancelled()
        final_response = state.attributes.get("final_response")
        finish_reason = state.attributes.get("finish_reason")
        pending_calls = state.agent.active_tool_call_ids
        passed = (
            isinstance(final_response, str)
            and bool(final_response.strip())
            and finish_reason in ("stop", "done")
            and not pending_calls
        )
        if not passed:
            return VerificationResult(
                VerificationStatus.FAILED,
                (),
                "The loop did not reach a defined terminal response.",
            )
        return VerificationResult(
            VerificationStatus.PASSED,
            (
                VerificationEvidence(
                    kind="agent-loop",
                    summary="Model stopped with no pending tool calls.",
                ),
            ),
            "Agent loop completion verified.",
        )


class CommandVerificationPolicy:
    """Runs the project's own checks and attributes every failure honestly."""

    def __init__(
        self,
        planner: VerificationPlanner,
        *,
        authorizer: CheckAuthorizer,
        event_bus: EventBus | None = None,
        integrity: ChangeIntegrityPolicy | None = None,
        check_timeout_seconds: float = _DEFAULT_CHECK_TIMEOUT,
    ) -> None:
        self.planner = planner
        #: Obligatorio y sin valor por defecto: cada entrada tiene que decir quien
        #: autoriza ejecutar codigo del proyecto. Un defecto permisivo es justo como la
        #: verificacion acabo ejecutando tests con la ejecucion desactivada (A01).
        self.authorizer = authorizer
        self.event_bus = event_bus
        self.integrity = integrity or ChangeIntegrityPolicy()
        self.check_timeout_seconds = check_timeout_seconds
        self.plan = planner.plan()
        self.baseline = Baseline()
        self._baseline_diff = ""

    async def capture_baseline(
        self, workspace: Workspace, cancellation: CancellationToken
    ) -> Baseline:
        """Run the plan before any change, so later failures can be attributed."""
        # La instantanea del diff va primero y sin pedir permiso: `git diff` con los
        # drivers externos apagados no ejecuta nada del proyecto, y sin ella la integridad
        # le atribuiria al run lo que la persona ya tenia cambiado (A19).
        self._baseline_diff = await self._git_diff(workspace, cancellation) or ""
        if self.plan.is_empty:
            return self.baseline
        if not await self.authorizer.authorize(
            self.plan.checks, workspace, cancellation, session_id="baseline"
        ):
            # Sin autorizacion no hay linea base: nada del proyecto se ejecuta.
            return self.baseline
        outcomes: dict[str, bool] = {}
        async with WORKSPACE_ACCESS.hold(workspace.root, write=False):
            for check in self.plan.checks:
                outcome = await self._run_check(
                    check, workspace, cancellation, session_id="baseline"
                )
                outcomes[check.name] = outcome.passed
        self.baseline = Baseline(outcomes, captured=True)
        return self.baseline

    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        # Se juzga un estado quieto: nadie escribe en la carpeta mientras se lee el diff y
        # corren los checks, o se aprobaria un estado distinto del que se comprobo (A09).
        async with WORKSPACE_ACCESS.hold(workspace.root, write=False):
            return await self._verify(state, workspace, cancellation)

    async def _verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        cancellation.raise_if_cancelled()
        session_id = state.session_id
        evidence: list[VerificationEvidence] = []

        integrity_findings = await self._inspect_integrity(workspace, cancellation)
        if integrity_findings:
            evidence.extend(
                VerificationEvidence(
                    kind=CheckKind.INTEGRITY.value,
                    summary=finding.detail,
                    metadata={"finding": finding.kind, "lines": list(finding.lines)},
                )
                for finding in integrity_findings
            )
            await self._publish(
                EventName.VERIFICATION_FAILED,
                session_id,
                {"reason": "integrity", "findings": [f.kind for f in integrity_findings]},
            )
            return VerificationResult(
                VerificationStatus.FAILED,
                tuple(evidence),
                "Verification refused: the change weakened the checks that verify it. "
                "Restore them, or ask for explicit authorization.",
            )

        if self.plan.is_empty:
            return VerificationResult(
                VerificationStatus.INCONCLUSIVE,
                (
                    VerificationEvidence(
                        kind="plan",
                        summary=(
                            "No verification command could be derived from AGENTS.md or the "
                            "project configuration, so completion cannot be proven."
                        ),
                        metadata=self.plan.describe(),
                    ),
                ),
                "Verification is inconclusive: the project defines no checks Athena may run.",
            )

        if not await self.authorizer.authorize(
            self.plan.checks, workspace, cancellation, session_id=session_id
        ):
            return VerificationResult(
                VerificationStatus.INCONCLUSIVE,
                (
                    VerificationEvidence(
                        kind=NOT_AUTHORIZED_EVIDENCE,
                        summary=(
                            "The project's checks were not run: local execution is "
                            "disabled for this run or was not authorized."
                        ),
                        metadata=self.plan.describe(),
                    ),
                ),
                "Verification is inconclusive: running the project's checks was not "
                "authorized, so nothing was executed and nothing is proven.",
            )

        introduced: list[str] = []
        pre_existing: list[str] = []
        unattributed: list[str] = []
        for check in self.plan.checks:
            outcome = await self._run_check(check, workspace, cancellation, session_id=session_id)
            was_passing = self.baseline.was_passing(check.name)
            attribution = _attribute(outcome.passed, was_passing)
            evidence.append(
                VerificationEvidence(
                    kind=check.kind.value,
                    summary=(
                        f"{check.name}: {'passed' if outcome.passed else 'failed'} ({attribution})"
                    ),
                    reference=check.rendered,
                    metadata={
                        **outcome.to_json(),
                        "attribution": attribution,
                        "baseline_passing": was_passing,
                        "output_tail": outcome.output_tail,
                    },
                )
            )
            if outcome.passed:
                continue
            if attribution == "introduced":
                introduced.append(check.name)
            elif attribution == "pre_existing":
                pre_existing.append(check.name)
            else:
                unattributed.append(check.name)

        if introduced:
            await self._publish(
                EventName.VERIFICATION_FAILED,
                session_id,
                {"reason": "introduced_failure", "checks": introduced},
            )
            return VerificationResult(
                VerificationStatus.FAILED,
                tuple(evidence),
                f"Checks broken by this change: {', '.join(introduced)}.",
            )
        if unattributed:
            await self._publish(
                EventName.VERIFICATION_FAILED,
                session_id,
                {"reason": "unattributed_failure", "checks": unattributed},
            )
            return VerificationResult(
                VerificationStatus.FAILED,
                tuple(evidence),
                (f"Failing checks with no baseline to compare against: {', '.join(unattributed)}."),
            )
        if pre_existing:
            still_red = (
                f"{len(pre_existing)} check(s) were already failing before this change "
                f"and are unchanged: {', '.join(pre_existing)}."
            )
            # Que sigan en rojo no se le imputa a este run —para eso esta la atribucion—
            # pero tampoco se puede encabezar el veredicto con «todas las comprobaciones
            # pasan». Eran dos frases seguidas, una falsa y otra verdadera, y la falsa iba
            # primera; la que se leia en una interfaz era la primera.
            if not _declared_paths(state):
                # Y sin un solo fichero tocado no hay nada que atribuir: el run no rompio
                # nada porque no hizo nada, y darlo por bueno convierte la verificacion en
                # un sello. Es el caso medido: `granite4.1:30b`, ante unos tests en rojo,
                # delego una tarea inventada, no cambio un fichero, y Athena publico
                # `agent.completed`.
                #
                # INCONCLUSIVE y no FAILED: no hay regresion que denunciar, y denunciarla
                # mandaria a buscar algo que no existe. Lo que pasa es que no se probo
                # nada (ADR-027).
                return VerificationResult(
                    VerificationStatus.INCONCLUSIVE,
                    tuple(evidence),
                    f"{still_red} This run changed no file, so nothing is proven either way.",
                )
            # Con trabajo hecho y sin regresiones, lo que se puede afirmar es eso y no mas.
            return VerificationResult(
                VerificationStatus.PASSED,
                tuple(evidence),
                f"This change broke no check. {still_red}",
            )
        return VerificationResult(
            VerificationStatus.PASSED, tuple(evidence), "All project checks pass."
        )

    # -- internals --------------------------------------------------------

    async def _inspect_integrity(
        self, workspace: Workspace, cancellation: CancellationToken
    ) -> tuple[IntegrityFinding, ...]:
        diff = await self._git_diff(workspace, cancellation)
        if diff is None:
            return ()
        return self.integrity.inspect(without_preexisting(diff, self._baseline_diff))

    async def _git_diff(self, workspace: Workspace, cancellation: CancellationToken) -> str | None:
        if not (workspace.root / ".git").exists():
            return None
        argv = (
            "git",
            "-c",
            f"safe.directory={workspace.root}",
            "-C",
            str(workspace.root),
            "diff",
            # Un driver de diff o un `textconv` configurado en el repositorio es un
            # comando que el proyecto elige: leer el diff no debe ejecutarlo.
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
        )
        try:
            _, stdout, _ = await run_process(
                argv, cwd=workspace.root, timeout_seconds=30.0, cancellation=cancellation
            )
        except AthenaRuntimeError:
            return None
        return stdout

    async def _run_check(
        self,
        check: VerificationCheck,
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        session_id: str,
    ) -> CheckOutcome:
        await self._publish(
            EventName.VERIFICATION_CHECK_STARTED,
            session_id,
            {"check": check.name, "kind": check.kind.value, "command": check.rendered},
        )
        started = time.monotonic()
        try:
            exit_code, stdout, stderr = await run_process(
                check.command,
                cwd=workspace.root,
                timeout_seconds=self.check_timeout_seconds,
                cancellation=cancellation,
                env=_CHECK_ENVIRONMENT,
            )
        except ProcessTimeoutError:
            outcome = CheckOutcome(
                check.name,
                check.kind,
                check.rendered,
                passed=False,
                exit_code=None,
                duration_seconds=round(time.monotonic() - started, 3),
                output_tail="The check timed out.",
            )
        else:
            combined = (stdout + stderr).strip()
            outcome = CheckOutcome(
                check.name,
                check.kind,
                check.rendered,
                passed=exit_code == 0,
                exit_code=exit_code,
                duration_seconds=round(time.monotonic() - started, 3),
                output_tail=combined[-_MAX_OUTPUT_TAIL:],
            )
        await self._publish(
            EventName.VERIFICATION_CHECK_COMPLETED,
            session_id,
            {
                "check": outcome.name,
                "passed": outcome.passed,
                "exit_code": outcome.exit_code,
                "duration_seconds": outcome.duration_seconds,
            },
        )
        return outcome

    async def _publish(self, name: EventName, session_id: str, payload: JSONObject) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.publish(VerificationEvent(name, session_id, payload))


def _attribute(passed: bool, was_passing: bool | None) -> str:
    if passed:
        return "passing"
    if was_passing is None:
        return "unattributed"
    return "introduced" if was_passing else "pre_existing"


def evidence_digest(result: VerificationResult, *, max_items: int = 6) -> str:
    """Compact failure evidence for the model. Full output stays out of the context."""
    lines = [f"Verification status: {result.status.value}", result.summary]
    for item in result.evidence[:max_items]:
        lines.append(f"- [{item.kind}] {item.summary}")
        tail = item.metadata.get("output_tail")
        if isinstance(tail, str) and tail and not str(item.summary).endswith("passing)"):
            lines.append(f"  output: {tail[-800:]}")
        detail = item.metadata.get("lines")
        if isinstance(detail, list) and detail:
            lines.extend(f"  {entry}" for entry in detail[:5])
    return "\n".join(lines)
