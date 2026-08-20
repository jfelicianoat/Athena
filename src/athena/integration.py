"""Bringing isolated writers back together, with git deciding what conflicts.

`isolation.py` gives each writer its own checkout and reports which files more than one of
them touched. Overlap is not conflict — two tasks can edit opposite ends of a file and
agree perfectly — so this is the step that finds out, and it finds out by asking git rather
than by reading the diffs itself.

The order of operations is the whole design:

1. **Check before applying.** `git apply --check --3way` says whether a patch would apply
   without touching anything. A workspace that has been half-integrated and then found to
   conflict is worse than one that was never touched, because the person now has to work
   out which half.
2. **Apply the ones that fit.** Cleanly, one at a time, so a later conflict does not
   invalidate an earlier success.
3. **Report the rest.** A conflicting patch is returned intact, with the files it wanted.
   Nothing is force-applied and nothing is silently dropped.
4. **Verify afterwards.** Every patch applying cleanly says nothing about whether the
   result works — that is exactly the case where two correct changes are wrong together,
   and it is the reason this step exists at all.

`git apply --3way` rather than `git merge`: a merge needs a clean tree, and the shared
workspace may legitimately be dirty from tasks that were never isolated. A patch touches
only the files it names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from athena.cancellation import CancellationToken
from athena.errors import AthenaRuntimeError, ToolExecutionError
from athena.isolation import IntegrationCandidate, overlapping_files
from athena.process_tools import run_process
from athena.types import JSONObject
from athena.verification import VerificationPolicy, VerificationResult
from athena.workspace import Workspace

_GIT_TIMEOUT_SECONDS = 120.0


class IntegrationError(AthenaRuntimeError):
    code = "integration_error"


class PatchOutcome(StrEnum):
    """What happened to one task's changes."""

    APPLIED = "applied"
    #: git could not reconcile it with what is already there. Left untouched, reported.
    CONFLICTED = "conflicted"
    #: Nothing to apply. Not a failure — a task can legitimately change nothing.
    EMPTY = "empty"
    #: Something went wrong invoking git. Distinct from a conflict, which is an answer.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PatchResult:
    task_id: str
    outcome: PatchOutcome
    files: tuple[str, ...] = ()
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.outcome is PatchOutcome.APPLIED

    def to_json(self) -> JSONObject:
        return {
            "task_id": self.task_id,
            "outcome": self.outcome.value,
            "files": list(self.files),
            "detail": self.detail[:400],
        }


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    """What came back together, what did not, and whether the result works."""

    patches: tuple[PatchResult, ...] = ()
    #: Files more than one task changed. Reported whether or not they conflicted, because
    #: "two people edited this" is worth a human's attention even when git coped.
    overlaps: JSONObject = field(default_factory=dict)
    verification: VerificationResult | None = None

    @property
    def applied(self) -> tuple[str, ...]:
        return tuple(patch.task_id for patch in self.patches if patch.applied)

    @property
    def conflicted(self) -> tuple[str, ...]:
        return tuple(
            patch.task_id for patch in self.patches if patch.outcome is PatchOutcome.CONFLICTED
        )

    @property
    def succeeded(self) -> bool:
        """Everything that had work applied, and the result proved itself.

        Both halves are required. All patches applying is not success — that is the case
        this module exists to distinguish from success.
        """
        if self.conflicted:
            return False
        if any(patch.outcome is PatchOutcome.FAILED for patch in self.patches):
            return False
        if self.verification is None:
            return False
        return self.verification.permits_completion

    def to_json(self) -> JSONObject:
        return {
            "patches": [patch.to_json() for patch in self.patches],
            "applied": list(self.applied),
            "conflicted": list(self.conflicted),
            "overlaps": dict(self.overlaps),
            "verification": (
                None
                if self.verification is None
                else {
                    "status": self.verification.status.value,
                    "summary": self.verification.summary,
                }
            ),
        }


class Integrator:
    """Applies isolated work back onto the shared workspace, and proves the result.

    Holds no policy about what *should* be integrated: it is given candidates and it
    reports what happened. Deciding to drop a conflicting task, replan it or ask a person
    belongs to whoever owns the graph.
    """

    def __init__(self, verification: VerificationPolicy | None = None) -> None:
        #: Run once, over the combined result. Without it this class can only say the
        #: patches applied, which is the weaker claim it exists to stop being mistaken for
        #: the stronger one.
        self.verification = verification

    async def integrate(
        self,
        workspace: Workspace,
        candidates: Sequence[IntegrationCandidate],
        cancellation: CancellationToken,
        *,
        session_id: str = "integration",
    ) -> IntegrationResult:
        overlaps = overlapping_files(candidates)
        patches: list[PatchResult] = []
        applied_any = False

        for candidate in candidates:
            cancellation.raise_if_cancelled()
            patch = await self._apply(workspace, candidate, cancellation)
            patches.append(patch)
            applied_any = applied_any or patch.applied

        verification = None
        if (
            self.verification is not None
            and applied_any
            and not any(patch.outcome is PatchOutcome.CONFLICTED for patch in patches)
        ):
            # Only worth proving a result that is actually assembled. Verifying a
            # half-integrated workspace would measure something nobody asked for and
            # attribute the failure to whichever task happened to apply.
            verification = await self._verify(workspace, cancellation, session_id)

        return IntegrationResult(
            patches=tuple(patches),
            overlaps={path: list(tasks) for path, tasks in overlaps.items()},
            verification=verification,
        )

    async def _apply(
        self,
        workspace: Workspace,
        candidate: IntegrationCandidate,
        cancellation: CancellationToken,
    ) -> PatchResult:
        if not candidate.diff.strip():
            return PatchResult(candidate.task_id, PatchOutcome.EMPTY)

        patch_file = workspace.root / f".athena-patch-{candidate.task_id}.diff"
        try:
            _write_patch(patch_file, candidate.diff)
        except OSError as exc:
            return PatchResult(candidate.task_id, PatchOutcome.FAILED, candidate.files, str(exc))

        try:
            # Checked first. A workspace that was half-integrated and then found to
            # conflict is worse than one nobody touched: the person now has to work out
            # which half.
            code, _, error = await _git(
                ("git", "apply", "--check", "--3way", str(patch_file)),
                workspace.root,
                cancellation,
            )
            if code != 0:
                return PatchResult(
                    candidate.task_id, PatchOutcome.CONFLICTED, candidate.files, error
                )
            code, _, error = await _git(
                ("git", "apply", "--3way", str(patch_file)), workspace.root, cancellation
            )
            if code != 0:
                # The check passed and the apply did not, which means the workspace moved
                # underneath us. Reported as a conflict because that is what it is.
                return PatchResult(
                    candidate.task_id, PatchOutcome.CONFLICTED, candidate.files, error
                )
        except (ToolExecutionError, OSError) as exc:
            return PatchResult(candidate.task_id, PatchOutcome.FAILED, candidate.files, str(exc))
        finally:
            patch_file.unlink(missing_ok=True)

        return PatchResult(candidate.task_id, PatchOutcome.APPLIED, candidate.files)

    async def _verify(
        self, workspace: Workspace, cancellation: CancellationToken, session_id: str
    ) -> VerificationResult | None:
        if self.verification is None:
            return None
        from athena.state import SessionState

        state = SessionState(session_id=session_id, workspace_id=workspace.workspace_id)
        return await self.verification.verify(state, workspace, cancellation)


def _write_patch(path: Path, diff: str) -> None:
    """Write a patch byte for byte.

        `Path.write_text` translates newlines on Windows, so a diff whose lines already end
        `

    ` — which every diff of a CRLF file does — comes back out as `


    ` and git
        rejects it with "patch does not apply". The failure is indistinguishable from a real
        conflict, which is what makes it worth writing bytes and never text.
    """
    path.write_bytes(diff.encode("utf-8"))


async def _git(
    argv: tuple[str, ...], cwd: Path, cancellation: CancellationToken
) -> tuple[int, str, str]:
    return await run_process(
        argv, cwd=cwd, timeout_seconds=_GIT_TIMEOUT_SECONDS, cancellation=cancellation
    )


async def revert_applied(
    workspace: Workspace,
    result: IntegrationResult,
    candidates: Sequence[IntegrationCandidate],
    cancellation: CancellationToken,
) -> tuple[str, ...]:
    """Undo an integration whose combined result did not work.

    Takes the candidates rather than reading them off the result, because
    `IntegrationResult` deliberately does not carry patch bodies — they can be large and a
    result ends up in events and logs. A revert that quietly did nothing because the text
    was missing would be worse than one that refuses.

    Reverses in the opposite order to applying, which is the only order in which
    overlapping hunks come back out cleanly. A patch that resists is left alone and
    reported by omission: an integration that cannot be undone is a fact somebody needs,
    not grounds for escalating to `git checkout`.
    """
    by_task = {candidate.task_id: candidate for candidate in candidates}
    reverted: list[str] = []
    for patch in reversed(result.patches):
        if not patch.applied:
            continue
        candidate = by_task.get(patch.task_id)
        if candidate is None or not candidate.diff.strip():
            continue
        patch_file = workspace.root / f".athena-revert-{patch.task_id}.diff"
        try:
            _write_patch(patch_file, candidate.diff)
            code, _, _ = await _git(
                ("git", "apply", "--reverse", "--3way", str(patch_file)),
                workspace.root,
                cancellation,
            )
            if code == 0:
                reverted.extend(patch.files)
        except (ToolExecutionError, OSError):
            continue
        finally:
            patch_file.unlink(missing_ok=True)
    return tuple(reverted)


__all__ = [
    "IntegrationError",
    "IntegrationResult",
    "Integrator",
    "PatchOutcome",
    "PatchResult",
    "revert_applied",
]
