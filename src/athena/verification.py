"""Evidence-based completion.

The model cannot declare its own work correct. A run may only finish after a
`VerificationPolicy` has produced evidence, and that evidence has to come from commands
the project itself defines — never from a command Athena invented.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.cancellation import CancellationToken
from athena.errors import AthenaRuntimeError, ProcessTimeoutError, WorkspaceBoundaryError
from athena.events import EventBus, EventName, VerificationEvent
from athena.permissions import RiskTier
from athena.process_tools import CommandPolicy, parse_command, run_process
from athena.state import SessionState
from athena.types import JSONObject
from athena.workspace import Workspace

_MAX_OUTPUT_TAIL = 2_000
_DEFAULT_CHECK_TIMEOUT = 300.0

#: A `.pyc` header stores the source mtime truncated to whole seconds, so two edits in the
#: same second that leave the file the same length look identical to the import system.
#: Athena edits fast and often keeps a file's length unchanged, which is exactly the shape
#: that makes a stale cache pass for fresh — and a verification judging the previous
#: version of the code is worse than no verification at all. Writing no bytecode during a
#: check means Athena can never create that trap for itself.
_CHECK_ENVIRONMENT = {"PYTHONDONTWRITEBYTECODE": "1"}


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class CheckKind(StrEnum):
    BUILD = "build"
    TEST = "test"
    LINT = "lint"
    TYPECHECK = "typecheck"
    DIFF_REVIEW = "diff_review"
    INTEGRITY = "integrity"


class PlanSource(StrEnum):
    EXPLICIT = "explicit_config"
    AGENTS_MD = "agents_md"
    PROJECT_CONFIG = "project_config"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    kind: str
    summary: str
    reference: str | None = None
    metadata: JSONObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    kind: CheckKind
    command: tuple[str, ...]
    required: bool = True

    @property
    def rendered(self) -> str:
        return " ".join(self.command)


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """The checks a run must survive, and where those commands came from."""

    checks: tuple[VerificationCheck, ...] = ()
    source: PlanSource = PlanSource.NONE

    @property
    def is_empty(self) -> bool:
        return not self.checks

    def describe(self) -> JSONObject:
        return {
            "source": self.source.value,
            "checks": [
                {"name": check.name, "kind": check.kind.value, "command": check.rendered}
                for check in self.checks
            ],
        }


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    name: str
    kind: CheckKind
    command: str
    passed: bool
    exit_code: int | None
    duration_seconds: float
    output_tail: str = ""

    def to_json(self) -> JSONObject:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "command": self.command,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class Baseline:
    """What the repository looked like before Athena touched it."""

    outcomes: Mapping[str, bool] = field(default_factory=dict)
    captured: bool = False

    def was_passing(self, name: str) -> bool | None:
        if not self.captured or name not in self.outcomes:
            return None
        return self.outcomes[name]


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    evidence: tuple[VerificationEvidence, ...]
    summary: str

    @property
    def permits_completion(self) -> bool:
        return self.status is VerificationStatus.PASSED and bool(self.evidence)


@runtime_checkable
class VerificationPolicy(Protocol):
    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult: ...


# --------------------------------------------------------------------------- planning


class VerificationPlanner:
    """Discovers verification commands. It never invents one.

    Sources, in precedence order:

    1. an explicit configuration passed by the operator;
    2. a `## Verification` section in the workspace `AGENTS.md`;
    3. the project's own configuration (`pyproject.toml`, `package.json`).

    Every candidate is parsed into argv and classified by `CommandPolicy`. Anything that
    is not plain local execution (R2) is discarded, so a malicious or careless
    instruction file cannot turn verification into an escape hatch.
    """

    _SECTION = re.compile(
        r"^##+\s*verification\s*$(.*?)(?=^##+\s|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL
    )
    _FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)

    def __init__(
        self,
        workspace: Workspace,
        *,
        command_policy: CommandPolicy | None = None,
        explicit: Sequence[VerificationCheck] = (),
    ) -> None:
        self.workspace = workspace
        self.command_policy = command_policy or CommandPolicy()
        self.explicit = tuple(explicit)

    def plan(self) -> VerificationPlan:
        if self.explicit:
            checks = self._accepted(self.explicit)
            if checks:
                return VerificationPlan(checks, PlanSource.EXPLICIT)
        from_instructions = self._accepted(self._from_agents_md())
        if from_instructions:
            return VerificationPlan(from_instructions, PlanSource.AGENTS_MD)
        detected = self._accepted(self._from_project_config())
        if detected:
            return VerificationPlan(detected, PlanSource.PROJECT_CONFIG)
        return VerificationPlan((), PlanSource.NONE)

    def _accepted(self, checks: Sequence[VerificationCheck]) -> tuple[VerificationCheck, ...]:
        allowed: list[VerificationCheck] = []
        for check in checks:
            if not check.command:
                continue
            classification = self.command_policy.classify(check.command, ".")
            if classification.tier is RiskTier.R2_LOCAL_EXECUTION:
                allowed.append(check)
        return tuple(allowed)

    def _from_agents_md(self) -> tuple[VerificationCheck, ...]:
        instructions = self.workspace.root / "AGENTS.md"
        if not instructions.is_file():
            return ()
        try:
            text = instructions.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ()
        section = self._SECTION.search(text)
        if section is None:
            return ()
        body = section.group(1)
        fenced = self._FENCE.search(body)
        lines = (fenced.group(1) if fenced else body).splitlines()
        checks: list[VerificationCheck] = []
        for raw in lines:
            line = raw.strip().lstrip("-").strip()
            if not line or line.startswith("#"):
                continue
            try:
                argv = parse_command(line)
            except AthenaRuntimeError:
                continue
            checks.append(VerificationCheck(line, _infer_kind(argv), argv))
        return tuple(checks)

    def _from_project_config(self) -> tuple[VerificationCheck, ...]:
        checks: list[VerificationCheck] = []
        checks.extend(self._from_pyproject())
        checks.extend(self._from_package_json())
        return tuple(checks)

    def _from_pyproject(self) -> tuple[VerificationCheck, ...]:
        config = self.workspace.root / "pyproject.toml"
        if not config.is_file():
            return ()
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            return ()
        tools = data.get("tool", {})
        if not isinstance(tools, dict):
            return ()
        checks: list[VerificationCheck] = []
        if "pytest" in tools:
            checks.append(
                VerificationCheck("pytest", CheckKind.TEST, ("python", "-m", "pytest", "-q"))
            )
        if "ruff" in tools:
            checks.append(
                VerificationCheck("ruff", CheckKind.LINT, ("python", "-m", "ruff", "check", "."))
            )
        if "mypy" in tools:
            checks.append(VerificationCheck("mypy", CheckKind.TYPECHECK, ("python", "-m", "mypy")))
        return tuple(checks)

    def _from_package_json(self) -> tuple[VerificationCheck, ...]:
        config = self.workspace.root / "package.json"
        if not config.is_file():
            return ()
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ()
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if not isinstance(scripts, dict):
            return ()
        mapping = {
            "test": (CheckKind.TEST, ("npm", "test")),
            "lint": (CheckKind.LINT, ("npm", "run", "lint")),
            "build": (CheckKind.BUILD, ("npm", "run", "build")),
        }
        return tuple(
            VerificationCheck(name, kind, command)
            for name, (kind, command) in mapping.items()
            if name in scripts
        )


def _infer_kind(argv: tuple[str, ...]) -> CheckKind:
    joined = " ".join(argv).lower()
    if "pytest" in joined or "test" in joined:
        return CheckKind.TEST
    if "mypy" in joined or "tsc" in joined:
        return CheckKind.TYPECHECK
    if "ruff" in joined or "lint" in joined or "eslint" in joined:
        return CheckKind.LINT
    return CheckKind.BUILD


# --------------------------------------------------------------------------- integrity


@dataclass(frozen=True, slots=True)
class IntegrityAuthorization:
    """Explicit permission to do the things that would otherwise look like cheating."""

    allow_test_removal: bool = False
    allow_test_skipping: bool = False
    allow_assertion_removal: bool = False
    allow_lint_suppression: bool = False


@dataclass(frozen=True, slots=True)
class IntegrityFinding:
    kind: str
    detail: str
    lines: tuple[str, ...]


class ChangeIntegrityPolicy:
    """Refuses a green verdict obtained by weakening what does the verifying."""

    _REMOVED_TEST = re.compile(r"^-\s*(?:async\s+)?(?:def\s+test_|it\(|test\()")
    _ADDED_TEST = re.compile(r"^\+\s*(?:async\s+)?(?:def\s+test_|it\(|test\()")
    _ADDED_SKIP = re.compile(
        r"^\+.*(pytest\.mark\.skip|pytest\.mark\.xfail|unittest\.skip|@skip\b"
        r"|\.skip\(|\.only\(|xit\(|xdescribe\()"
    )
    _REMOVED_ASSERT = re.compile(r"^-\s*(assert\b|expect\(|self\.assert)")
    _ADDED_ASSERT = re.compile(r"^\+\s*(assert\b|expect\(|self\.assert)")
    _ADDED_SUPPRESSION = re.compile(
        r"^\+.*(#\s*noqa|#\s*type:\s*ignore|--exit-zero|ignore_errors\s*=\s*true"
        r"|--no-verify|eslint-disable|# ruff: noqa)"
    )

    def __init__(self, authorization: IntegrityAuthorization | None = None) -> None:
        self.authorization = authorization or IntegrityAuthorization()

    def inspect(self, diff: str) -> tuple[IntegrityFinding, ...]:
        removed_tests: list[str] = []
        added_tests = 0
        added_skips: list[str] = []
        removed_assertions = 0
        added_assertions = 0
        removed_assertion_lines: list[str] = []
        suppressions: list[str] = []
        for line in diff.splitlines():
            if line.startswith("---") or line.startswith("+++"):
                continue
            if self._REMOVED_TEST.match(line):
                removed_tests.append(line.strip())
            if self._ADDED_TEST.match(line):
                added_tests += 1
            if self._ADDED_SKIP.match(line):
                added_skips.append(line.strip())
            if self._REMOVED_ASSERT.match(line):
                removed_assertions += 1
                removed_assertion_lines.append(line.strip())
            if self._ADDED_ASSERT.match(line):
                added_assertions += 1
            if self._ADDED_SUPPRESSION.match(line):
                suppressions.append(line.strip())

        findings: list[IntegrityFinding] = []
        # Net counting, so renaming or restructuring a test is not mistaken for deleting it.
        net_removed_tests = len(removed_tests) - added_tests
        if net_removed_tests > 0 and not self.authorization.allow_test_removal:
            findings.append(
                IntegrityFinding(
                    "test_removed",
                    f"{net_removed_tests} test definition(s) were deleted",
                    tuple(removed_tests[:10]),
                )
            )
        if added_skips and not self.authorization.allow_test_skipping:
            findings.append(
                IntegrityFinding(
                    "test_skipped",
                    f"{len(added_skips)} test(s) were skipped or narrowed",
                    tuple(added_skips[:10]),
                )
            )
        if removed_assertions > added_assertions and not self.authorization.allow_assertion_removal:
            findings.append(
                IntegrityFinding(
                    "assertions_weakened",
                    f"{removed_assertions - added_assertions} assertion(s) were removed",
                    tuple(removed_assertion_lines[:10]),
                )
            )
        if suppressions and not self.authorization.allow_lint_suppression:
            findings.append(
                IntegrityFinding(
                    "checks_suppressed",
                    f"{len(suppressions)} lint or type suppression(s) were added",
                    tuple(suppressions[:10]),
                )
            )
        return tuple(findings)


# --------------------------------------------------------------------------- policies


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
        event_bus: EventBus | None = None,
        integrity: ChangeIntegrityPolicy | None = None,
        check_timeout_seconds: float = _DEFAULT_CHECK_TIMEOUT,
    ) -> None:
        self.planner = planner
        self.event_bus = event_bus
        self.integrity = integrity or ChangeIntegrityPolicy()
        self.check_timeout_seconds = check_timeout_seconds
        self.plan = planner.plan()
        self.baseline = Baseline()

    async def capture_baseline(
        self, workspace: Workspace, cancellation: CancellationToken
    ) -> Baseline:
        """Run the plan before any change, so later failures can be attributed."""
        if self.plan.is_empty:
            return self.baseline
        outcomes: dict[str, bool] = {}
        for check in self.plan.checks:
            outcome = await self._run_check(check, workspace, cancellation, session_id="baseline")
            outcomes[check.name] = outcome.passed
        self.baseline = Baseline(outcomes, captured=True)
        return self.baseline

    async def verify(
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
        summary = "All project checks pass."
        if pre_existing:
            summary += (
                f" {len(pre_existing)} check(s) were already failing before this change "
                f"and are unchanged: {', '.join(pre_existing)}."
            )
        return VerificationResult(VerificationStatus.PASSED, tuple(evidence), summary)

    # -- internals --------------------------------------------------------

    async def _inspect_integrity(
        self, workspace: Workspace, cancellation: CancellationToken
    ) -> tuple[IntegrityFinding, ...]:
        diff = await self._git_diff(workspace, cancellation)
        if diff is None:
            return ()
        return self.integrity.inspect(diff)

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


class ArtifactVerificationPolicy:
    """Evidencia para un dominio donde no hay comandos que ejecutar.

    No todo trabajo se comprueba corriendo algo. Un encargo cuyo resultado es un documento
    no tiene suite que pase ni compilador que se queje, y hasta ahora eso significaba que
    Athena no podia terminar nunca: sin plan de verificacion la unica salida era
    «inconclusive», que tras ADR-027 es exactamente lo que hay que decir cuando no se pudo
    comprobar nada. Correcto, y ademas inutil como unico final posible.

    Asi que aqui la evidencia es otra: **los entregables existen, no estan vacios y este
    run los toco**. Es deterministico, lo produce el runtime y no depende de que el modelo
    diga que ha terminado, que es lo que exige el contrato de verificacion.

    Lo que NO demuestra hay que decirlo igual de claro, porque un verde que se lee como
    mas de lo que es hace mas daño que un rojo: esto prueba que algo se produjo, no que
    sea bueno. Ningun automatismo puede juzgar si un documento cumple su encargo, y fingir
    que si convertiria la verificacion en un sello.
    """

    #: Lo que esta politica afirma cuando pasa, para que viaje con la evidencia.
    PROVES = (
        "The declared deliverables exist, are non-empty and were written by this run. "
        "It does not establish that their content is correct."
    )

    def __init__(self, expected: Sequence[str] = ()) -> None:
        #: Entregables pedidos por quien encargo el trabajo. Sin ellos se comprueba lo
        #: que el run declara haber escrito, que es mas debil y se reporta como tal.
        self.expected = tuple(expected)

    async def verify(
        self, state: SessionState, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult:
        cancellation.raise_if_cancelled()
        written = _declared_paths(state)
        targets = self.expected or written
        if not targets:
            # Ni se pidio nada concreto ni el run escribio nada. No hay con que
            # demostrar ni exito ni fracaso, y decir cualquiera de los dos seria
            # inventarselo.
            return VerificationResult(
                VerificationStatus.INCONCLUSIVE,
                (
                    VerificationEvidence(
                        kind="artifact",
                        summary=(
                            "Nothing was produced and no deliverable was declared, so "
                            "there is nothing to show either way."
                        ),
                        metadata={"expected": [], "written": []},
                    ),
                ),
                "Verification is inconclusive: no deliverable was produced or declared.",
            )
        evidence: list[VerificationEvidence] = []
        missing: list[str] = []
        for relative in targets:
            cancellation.raise_if_cancelled()
            try:
                path = workspace.resolve(relative, must_exist=False)
            except WorkspaceBoundaryError:
                missing.append(relative)
                continue
            exists = path.is_file()
            size = path.stat().st_size if exists else 0
            touched = relative in written
            passed = exists and size > 0 and (touched or not self.expected)
            if not passed:
                missing.append(relative)
            evidence.append(
                VerificationEvidence(
                    kind="artifact",
                    summary=f"{relative}: {'produced' if passed else 'not produced'}",
                    reference=relative,
                    metadata={
                        "name": relative,
                        "passed": passed,
                        "exists": exists,
                        "size_bytes": size,
                        "written_by_this_run": touched,
                    },
                )
            )
        if missing:
            return VerificationResult(
                VerificationStatus.FAILED,
                tuple(evidence),
                "Verification failed: "
                + ", ".join(sorted(missing))
                + " was not produced as a non-empty file.",
            )
        return VerificationResult(
            VerificationStatus.PASSED,
            tuple(evidence),
            f"{len(evidence)} deliverable(s) produced. {self.PROVES}",
        )


def _declared_paths(state: SessionState) -> tuple[str, ...]:
    raw = state.attributes.get("files_modified")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


__all__ = [
    "ArtifactVerificationPolicy",
    "Baseline",
    "ChangeIntegrityPolicy",
    "CheckKind",
    "CheckOutcome",
    "CommandVerificationPolicy",
    "IntegrityAuthorization",
    "IntegrityFinding",
    "LoopCompletionVerificationPolicy",
    "PlanSource",
    "VerificationCheck",
    "VerificationEvidence",
    "VerificationPlan",
    "VerificationPlanner",
    "VerificationPolicy",
    "VerificationResult",
    "VerificationStatus",
    "evidence_digest",
]
