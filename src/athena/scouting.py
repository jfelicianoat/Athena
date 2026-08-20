"""Working out whether a goal needs a plan, by looking at the repository.

`DecompositionPolicy` decides deterministically and always has. What it could not do was
get its evidence: somebody had to supply `DecompositionSignals`, which meant "does this
need decomposing" was answered with facts a caller gathered by hand — and in practice
guessed.

This gathers what can honestly be gathered, and says which signals it could not establish.
That second half is the design. A scout that filled every field with a default would be
handing the policy invented evidence dressed as measurement, and the policy has no way to
tell the difference — so an unestablished signal keeps its neutral value *and* is named, so
a caller can supply it, ask a person, or accept a decision made on less.

Nothing here reads a model. It reads paths, file sizes and the project's own verification
plan; the objective is used only to find which paths it names, never interpreted for
intent. A repository is evidence and a sentence is not.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from athena.errors import AthenaRuntimeError, WorkspaceBoundaryError
from athena.planning import DecompositionSignals
from athena.types import JSONObject
from athena.verification import VerificationPlanner
from athena.workspace import Workspace

#: Directories that are never the subject of an engineering goal, whatever a path says.
_IGNORED = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

#: A token that looks like it names a file. Deliberately narrow: `calc.py`, `src/api.rs`,
#: `athena/planning`. Anything vaguer is somebody's prose and is not treated as evidence.
_PATH_LIKE = re.compile(r"[\w./\\-]*[\w-]+\.[A-Za-z0-9]{1,6}|[\w-]+/[\w./-]+")

#: Above this, a change is large enough that touching it blind is a real risk. Chosen to be
#: the size at which a file stops fitting in one reading, not from any theory.
_LARGE_FILE_LINES = 600

#: How many files may be walked before the scout gives up on counting.
_MAX_SCANNED = 4_000


@dataclass(frozen=True, slots=True)
class ScoutedSignals:
    """What was measured, what was assumed, and the signals themselves."""

    signals: DecompositionSignals
    #: Signals this scout genuinely established from the repository.
    established: tuple[str, ...] = ()
    #: Signals it could not, which keep their neutral value. Named so nobody mistakes a
    #: default for a finding.
    assumed: tuple[str, ...] = field(default_factory=tuple)
    #: Paths in the objective that resolved to something that exists.
    resolved_paths: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.assumed

    def to_json(self) -> JSONObject:
        return {
            "independently_verifiable_outputs": (self.signals.independently_verifiable_outputs),
            "has_meaningful_dependencies": self.signals.has_meaningful_dependencies,
            "parallelisable_investigation": self.signals.parallelisable_investigation,
            "high_implementation_risk": self.signals.high_implementation_risk,
            "subsystems_touched": self.signals.subsystems_touched,
            "distinct_roles_required": self.signals.distinct_roles_required,
            "established": list(self.established),
            "assumed": list(self.assumed),
            "resolved_paths": list(self.resolved_paths),
            "notes": list(self.notes),
        }

    def explain(self) -> str:
        """What was measured and what was not, in a sentence somebody can act on."""
        lines = [f"Established from the repository: {', '.join(self.established) or 'nothing'}."]
        if self.assumed:
            lines.append(
                "Not established, left at their neutral value: " + ", ".join(self.assumed) + "."
            )
        lines.extend(self.notes)
        return " ".join(lines)


class RepositoryScout:
    """Reads a workspace and an objective, and reports what it can actually tell.

    Everything it measures is a fact about files. It does not ask a model, and it does not
    read the objective for intent — only for path-shaped tokens, because a path either
    resolves in the repository or it does not, and that is checkable in a way that "this
    sounds ambitious" is not.
    """

    def __init__(self, *, large_file_lines: int = _LARGE_FILE_LINES) -> None:
        self.large_file_lines = large_file_lines

    def scout(self, workspace: Workspace, objective: str) -> ScoutedSignals:
        paths = self._resolve_paths(workspace, objective)
        subsystems = self._subsystems(workspace, paths)
        checks = self._verification_checks(workspace)
        risky, notes = self._risk(workspace, paths)
        parallel = len(paths) > 1 or (not paths and subsystems > 1)

        established = ["subsystems_touched", "parallelisable_investigation"]
        assumed = ["has_meaningful_dependencies", "distinct_roles_required"]

        # Outputs are countable only when the project defines its own checks: several
        # independent checks means several things that can be proved apart. Without a
        # verification plan there is nothing to count, and guessing would decide the whole
        # question — it is the gate the policy weighs most.
        if checks > 0:
            outputs = min(checks, max(1, subsystems))
            established.append("independently_verifiable_outputs")
        else:
            outputs = 1
            assumed.append("independently_verifiable_outputs")
            notes.append(
                "The project defines no verification commands, so how many outputs could "
                "be proved separately is unknown."
            )

        if risky is None:
            assumed.append("high_implementation_risk")
            high_risk = False
        else:
            established.append("high_implementation_risk")
            high_risk = risky

        return ScoutedSignals(
            signals=DecompositionSignals(
                independently_verifiable_outputs=outputs,
                has_meaningful_dependencies=False,
                parallelisable_investigation=parallel,
                high_implementation_risk=high_risk,
                subsystems_touched=subsystems,
                distinct_roles_required=1,
            ),
            established=tuple(sorted(established)),
            assumed=tuple(sorted(assumed)),
            resolved_paths=paths,
            notes=tuple(notes),
        )

    # -- measurements ------------------------------------------------------

    def _resolve_paths(self, workspace: Workspace, objective: str) -> tuple[str, ...]:
        """Path-shaped tokens in the objective that exist in the repository.

        Resolution is the filter. A token that names nothing is somebody writing prose
        about a concept, and counting it would let a wordy objective look like a wide one.
        """
        found: dict[str, None] = {}
        for token in _PATH_LIKE.findall(objective):
            candidate = token.strip().strip(".,;:()[]'\"").replace("\\", "/")
            if not candidate or candidate.startswith("http"):
                continue
            try:
                resolved = workspace.resolve(candidate, must_exist=True)
            except (WorkspaceBoundaryError, OSError, ValueError):
                # Not a path in this repository. That is the filter, not an error: an
                # objective mentioning `README.md` from another project is prose here.
                continue
            found[str(resolved.relative_to(workspace.root)).replace("\\", "/")] = None
        return tuple(found)

    def _subsystems(self, workspace: Workspace, paths: Sequence[str]) -> int:
        """How many distinct parts of the repository are in play.

        When the objective names files, it is how many directories they live in. When it
        names none, it is how many top-level source directories the repository has — which
        is a statement about the repository's breadth rather than the goal's, and is
        treated as the weaker evidence it is.
        """
        if paths:
            return max(1, len({Path(path).parent.as_posix() for path in paths}))
        return max(1, len(_source_directories(workspace.root)))

    def _verification_checks(self, workspace: Workspace) -> int:
        """How many checks the project defines, from its own configuration.

        Read through `VerificationPlanner`, so the scout counts exactly what a run would
        execute. Counting something else would make the signal disagree with the thing it
        is a signal about.
        """
        try:
            plan = VerificationPlanner(workspace).plan()
        except AthenaRuntimeError:
            # A project whose configuration cannot be read defines no checks as far as this
            # is concerned. It is not the scout's job to explain why.
            return 0
        return len(plan.checks)

    def _risk(self, workspace: Workspace, paths: Sequence[str]) -> tuple[bool | None, list[str]]:
        """Whether the named files are large enough that changing them blind is risky.

        `None` when the objective named no files: risk is a property of what is being
        changed, and with nothing named there is nothing to measure. Returning `False`
        would be asserting safety on no evidence.
        """
        notes: list[str] = []
        if not paths:
            return None, notes
        for path in paths:
            target = workspace.root / path
            if not target.is_file():
                continue
            try:
                lines = sum(1 for _ in target.open("r", encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if lines > self.large_file_lines:
                notes.append(f"{path} is {lines} lines, large enough to change carefully.")
                return True, notes
        return False, notes


def _source_directories(root: Path) -> tuple[str, ...]:
    """Top-level directories that plausibly hold source.

    Bounded and shallow on purpose: this is a rough measure of breadth, and walking a large
    repository to refine it would cost more than the answer is worth.
    """
    directories: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return ()
    for entry in entries[:_MAX_SCANNED]:
        if not entry.is_dir() or entry.name in _IGNORED or entry.name.startswith("."):
            continue
        if _holds_source(entry):
            directories.append(entry.name)
    return tuple(directories)


def _holds_source(directory: Path) -> bool:
    try:
        for candidate in directory.rglob("*"):
            if candidate.is_file() and candidate.suffix in {
                ".py",
                ".rs",
                ".ts",
                ".tsx",
                ".js",
                ".go",
                ".java",
                ".rb",
                ".c",
                ".cpp",
                ".cs",
            }:
                return True
    except OSError:
        return False
    return False


def merge(scouted: ScoutedSignals, supplied: DecompositionSignals) -> DecompositionSignals:
    """Let a caller fill in what the scout could not, without overriding what it measured.

    The direction is the point. A caller knows things the repository cannot show — that
    two outputs genuinely depend on each other, that the work needs an investigator and an
    implementer — and it does not know better than the filesystem how many files exist.
    """
    established = set(scouted.established)
    measured = scouted.signals
    return DecompositionSignals(
        independently_verifiable_outputs=(
            measured.independently_verifiable_outputs
            if "independently_verifiable_outputs" in established
            else supplied.independently_verifiable_outputs
        ),
        has_meaningful_dependencies=(
            measured.has_meaningful_dependencies
            if "has_meaningful_dependencies" in established
            else supplied.has_meaningful_dependencies
        ),
        parallelisable_investigation=(
            measured.parallelisable_investigation
            if "parallelisable_investigation" in established
            else supplied.parallelisable_investigation
        ),
        high_implementation_risk=(
            measured.high_implementation_risk
            if "high_implementation_risk" in established
            else supplied.high_implementation_risk
        ),
        subsystems_touched=(
            measured.subsystems_touched
            if "subsystems_touched" in established
            else supplied.subsystems_touched
        ),
        distinct_roles_required=(
            measured.distinct_roles_required
            if "distinct_roles_required" in established
            else supplied.distinct_roles_required
        ),
    )


__all__ = ["RepositoryScout", "ScoutedSignals", "merge"]
