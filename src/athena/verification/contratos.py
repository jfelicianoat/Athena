"""Que es una verificacion: comprobaciones, evidencia, linea base y resultado.

El resultado distingue fallo de indeterminado. No es un matiz: un check que
no se pudo ejecutar no autoriza a completar, pero tampoco acusa al trabajo.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.cancellation import CancellationToken
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
