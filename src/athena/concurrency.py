"""Deciding what may run at the same time.

The cheap answer is "different tools, so go ahead". It is also wrong: two different tools
can both be writing the same file, and two reads of a file a third call is rewriting are
not independent either. Safety here is a property of the *resources* a call touches, not of
which tool touches them.

So a call runs in parallel only when both hold:

1. **both** tools declare themselves concurrency-safe for those exact arguments, and
2. if either writes, their resources do not overlap.

The first condition does the real work. Two writes to different files are still
serialised, because both tools said they were not safe to run alongside anything, and a
pair of distinct paths is not a stronger claim than that.

Anything that mutates git, or operates on the workspace as a whole, takes the workspace
exclusively. When in doubt the scheduler serialises, because a wrong parallel decision
corrupts a repository while a wrong serial decision only costs time.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from athena.tools import Tool
from athena.types import JSONObject

#: Resource token meaning "the workspace as a whole".
WORKSPACE = "workspace:*"

#: Tools whose effect is not confined to the paths in their arguments.
_WHOLE_WORKSPACE_TOOLS = frozenset({"git_commit", "bash"})

#: Argument keys that name a resource a call touches.
_PATH_KEYS = ("path", "paths", "cwd", "glob", "pattern")


class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"
    #: Takes the whole workspace; nothing else may run alongside it.
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    """What one call needs, and how."""

    call_id: str
    tool_name: str
    mode: AccessMode
    resources: frozenset[str] = field(default_factory=frozenset)
    concurrency_safe: bool = False

    def conflicts_with(self, other: ResourceClaim) -> bool:
        if self.mode is AccessMode.EXCLUSIVE or other.mode is AccessMode.EXCLUSIVE:
            return True
        # The tool's own declaration is the primary gate. Two writes to different files
        # are still serialised, because both tools said they are not safe to run
        # alongside anything — and their paths are not a stronger claim than that.
        if not (self.concurrency_safe and other.concurrency_safe):
            return True
        if self.mode is AccessMode.WRITE or other.mode is AccessMode.WRITE:
            if WORKSPACE in self.resources or WORKSPACE in other.resources:
                return True
            return bool(self.resources & other.resources)
        return False


def claim_for(tool: Tool, call_id: str, arguments: JSONObject) -> ResourceClaim:
    """Work out what a call touches, erring towards claiming too much."""
    name = tool.spec.name
    read_only = _safe_call(tool.is_read_only, arguments, default=False)
    destructive = _safe_call(tool.is_destructive, arguments, default=True)
    concurrency_safe = _safe_call(tool.is_concurrency_safe, arguments, default=False)
    resources = _resources(arguments)

    if name in _WHOLE_WORKSPACE_TOOLS or destructive:
        # A command or a commit can touch anything; its arguments do not bound it.
        return ResourceClaim(call_id, name, AccessMode.EXCLUSIVE, frozenset({WORKSPACE}), False)
    if read_only:
        # A read with no identifiable resource could be reading anything, so it is only
        # parallelisable against other reads — which is what READ already means.
        return ResourceClaim(
            call_id, name, AccessMode.READ, resources or frozenset({WORKSPACE}), concurrency_safe
        )
    return ResourceClaim(
        call_id, name, AccessMode.WRITE, resources or frozenset({WORKSPACE}), False
    )


def _safe_call(predicate: object, arguments: JSONObject, *, default: bool) -> bool:
    """A tool that cannot answer for these arguments gets the cautious answer."""
    if not callable(predicate):
        return default
    try:
        return bool(predicate(arguments))
    except Exception:
        return default


def _resources(arguments: JSONObject) -> frozenset[str]:
    found: set[str] = set()
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            found.add(_normalise(value))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            found.update(_normalise(item) for item in value if isinstance(item, str) and item)
    return frozenset(found)


def _normalise(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").casefold()


@dataclass(frozen=True, slots=True)
class ScheduledBatch:
    """One wave of calls that may run together, in order."""

    claims: tuple[ResourceClaim, ...]

    @property
    def call_ids(self) -> tuple[str, ...]:
        return tuple(claim.call_id for claim in self.claims)

    @property
    def parallel(self) -> bool:
        return len(self.claims) > 1


class ConcurrencyScheduler:
    """Groups a turn's calls into waves that are safe to run together.

    Order within the original batch is preserved: a call never overtakes one it conflicts
    with, so a read scheduled after a write to the same file still sees the write.
    """

    def __init__(self, *, max_parallel: int = 4) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        self.max_parallel = max_parallel

    def plan(self, claims: Iterable[ResourceClaim]) -> tuple[ScheduledBatch, ...]:
        batches: list[list[ResourceClaim]] = []
        for claim in claims:
            if claim.mode is AccessMode.EXCLUSIVE:
                batches.append([claim])
                continue
            # A call may not overtake anything it conflicts with, so the earliest wave it
            # can join is the one after the last conflicting wave. Every later wave is
            # conflict-free by construction.
            earliest = 0
            for index, batch in enumerate(batches):
                if any(claim.conflicts_with(existing) for existing in batch):
                    earliest = index + 1
            placed = False
            for index in range(earliest, len(batches)):
                if len(batches[index]) < self.max_parallel:
                    batches[index].append(claim)
                    placed = True
                    break
            if not placed:
                batches.append([claim])
        return tuple(ScheduledBatch(tuple(batch)) for batch in batches)

    def plan_calls(
        self, calls: Sequence[tuple[str, Tool, JSONObject]]
    ) -> tuple[ScheduledBatch, ...]:
        return self.plan(claim_for(tool, call_id, arguments) for call_id, tool, arguments in calls)


__all__ = [
    "WORKSPACE",
    "AccessMode",
    "ConcurrencyScheduler",
    "ResourceClaim",
    "ScheduledBatch",
    "claim_for",
]
