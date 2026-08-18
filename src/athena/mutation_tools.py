"""Workspace-confined mutation tools with atomic writes and diff evidence."""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path
from typing import ClassVar

from athena.cancellation import CancellationToken
from athena.errors import ToolExecutionError, ToolValidationError
from athena.events import EventBus, EventName, FileEvent
from athena.permissions import PermissionRequest, RiskLevel, RiskTier
from athena.tools import Tool, ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject

_MAX_WRITE_CHARS = 1_000_000
_MAX_DIFF_CHARS = 8_000
#: Replacing more than this fraction of an existing file looks like a truncated payload.
_SUSPICIOUS_SHRINK_RATIO = 0.5


def _reject_unknown(arguments: JSONObject, allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise ToolValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")


def _string(arguments: JSONObject, name: str, *, allow_empty: bool = False) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolValidationError(f"{name} must be a string")
    if not value and not allow_empty:
        raise ToolValidationError(f"{name} must be non-empty")
    return value


def _flag(arguments: JSONObject, name: str) -> bool:
    value = arguments.get(name, False)
    if not isinstance(value, bool):
        raise ToolValidationError(f"{name} must be a boolean")
    return value


def _occurrences(arguments: JSONObject) -> int:
    value = arguments.get("expected_occurrences", 1)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolValidationError("expected_occurrences must be an integer")
    if value < 1 or value > 100:
        raise ToolValidationError("expected_occurrences must be between 1 and 100")
    return value


def _read_existing(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ToolValidationError(f"Not a regular file: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ToolExecutionError(f"Cannot read file for modification: {path.name}") from exc


def _atomic_write(path: Path, content: str) -> None:
    """Write through a sibling temp file so a failure never leaves a partial file."""
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".athena-tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ToolExecutionError(f"Atomic write failed for {path.name}") from exc


def _unified_diff(before: str | None, after: str, relative_path: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative_path}" if before is not None else "/dev/null",
            tofile=f"b/{relative_path}",
            n=3,
        )
    )
    if len(diff) > _MAX_DIFF_CHARS:
        return diff[:_MAX_DIFF_CHARS] + "\n...[diff truncated]"
    return diff


def _relative_label(context: ToolContext, path: Path) -> str:
    """Workspace-relative label that also works for a file that does not exist yet."""
    try:
        return path.relative_to(context.workspace.root).as_posix()
    except ValueError:
        return path.name


class _MutationTool:
    """Shared behaviour: workspace confinement, evidence, and file.changed emission."""

    spec: ClassVar[ToolSpec]

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self.event_bus = event_bus

    def is_read_only(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        """Writes never declare themselves safe to run alongside another call."""
        del arguments
        return False

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    async def _record_change(
        self,
        context: ToolContext,
        relative_path: str,
        change: str,
        diff: str,
        before: str | None,
        after: str,
    ) -> JSONObject:
        evidence: JSONObject = {
            "path": relative_path,
            "change": change,
            "diff": diff,
            "chars_before": len(before) if before is not None else 0,
            "chars_after": len(after),
            "lines_before": len((before or "").splitlines()),
            "lines_after": len(after.splitlines()),
        }
        if self.event_bus is not None:
            await self.event_bus.publish(
                FileEvent(
                    EventName.FILE_CHANGED,
                    context.session_id,
                    evidence,
                    context.call_id,
                )
            )
        return evidence


class WriteFileTool(_MutationTool):
    spec = ToolSpec(
        name="write_file",
        description=(
            "Create a file, or fully replace one with complete content. "
            "Overwriting requires overwrite=true; use edit_file for partial changes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
                "allow_empty": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        risk=RiskLevel.MEDIUM,
        max_result_size_chars=12_000,
        search_hint="create a new file or fully rewrite a small one",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        _reject_unknown(arguments, {"path", "content", "overwrite", "allow_empty"})
        allow_empty = _flag(arguments, "allow_empty")
        content = _string(arguments, "content", allow_empty=True)
        if not content and not allow_empty:
            raise ToolValidationError(
                "content is empty; pass allow_empty=true to intentionally write an empty file"
            )
        if len(content) > _MAX_WRITE_CHARS:
            raise ToolValidationError("content exceeds the maximum writable size")
        return {
            "path": _string(arguments, "path"),
            "content": content,
            "overwrite": _flag(arguments, "overwrite"),
            "allow_empty": allow_empty,
        }

    def is_destructive(self, arguments: JSONObject) -> bool:
        """Coarse, context-free hint. permission() computes the precise verdict."""
        return _flag(arguments, "overwrite")

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        path = context.workspace.resolve(_string(arguments, "path"), must_exist=False)
        relative = _relative_label(context, path)
        content = arguments.get("content")
        after = len(content) if isinstance(content, str) else 0
        existing = _read_existing(path)
        before = len(existing) if existing is not None else 0
        destructive = existing is not None and after < before * _SUSPICIOUS_SHRINK_RATIO
        effects = [
            f"{'Replaces' if existing is not None else 'Creates'} {relative} in the workspace",
            f"File size {before} -> {after} characters",
        ]
        if destructive:
            effects.append("Discards more than half of the current file content")
        return PermissionRequest(
            tool_name=self.spec.name,
            operation="write_file",
            action=f"{'overwrite' if existing is not None else 'create'} {relative}",
            workspace=context.workspace,
            risk=RiskLevel.HIGH if destructive else RiskLevel.MEDIUM,
            tier=RiskTier.R1_WORKSPACE_WRITE,
            is_read_only=False,
            is_destructive=destructive,
            is_concurrency_safe=False,
            reason="The agent requested a full-content write inside the workspace.",
            possible_effects=tuple(effects),
            resources=(str(path),),
            arguments=arguments,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        cancellation.raise_if_cancelled()
        raw_path = _string(arguments, "path")
        content = _string(arguments, "content", allow_empty=True)
        path = context.workspace.resolve(raw_path, must_exist=False)
        before = _read_existing(path)
        if before is not None and not _flag(arguments, "overwrite"):
            raise ToolValidationError(
                f"{raw_path} already exists; pass overwrite=true to replace it"
            )
        if not path.parent.is_dir():
            raise ToolValidationError(f"Parent directory does not exist: {raw_path}")
        cancellation.raise_if_cancelled()
        _atomic_write(path, content)
        relative = context.workspace.relative(path)
        diff = _unified_diff(before, content, relative)
        evidence = await self._record_change(
            context,
            relative,
            "modified" if before is not None else "created",
            diff,
            before,
            content,
        )
        return ToolResult(evidence)


class EditFileTool(_MutationTool):
    spec = ToolSpec(
        name="edit_file",
        description=(
            "Replace an exact literal string in an existing file. "
            "The match must occur exactly expected_occurrences times."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        risk=RiskLevel.MEDIUM,
        max_result_size_chars=12_000,
        search_hint="make a surgical change to an existing file",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        _reject_unknown(arguments, {"path", "old_string", "new_string", "expected_occurrences"})
        old = _string(arguments, "old_string")
        new = _string(arguments, "new_string", allow_empty=True)
        if old == new:
            raise ToolValidationError("old_string and new_string must differ")
        return {
            "path": _string(arguments, "path"),
            "old_string": old,
            "new_string": new,
            "expected_occurrences": _occurrences(arguments),
        }

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        path = context.workspace.resolve(_string(arguments, "path"))
        relative = context.workspace.relative(path)
        expected = _occurrences(arguments)
        return PermissionRequest(
            tool_name=self.spec.name,
            operation="edit_file",
            action=f"replace {expected} occurrence(s) in {relative}",
            workspace=context.workspace,
            risk=RiskLevel.MEDIUM,
            tier=RiskTier.R1_WORKSPACE_WRITE,
            is_read_only=False,
            is_destructive=False,
            is_concurrency_safe=False,
            reason="The agent requested a literal in-place replacement inside the workspace.",
            possible_effects=(
                f"Modifies {relative} in place",
                "Leaves every other file untouched",
            ),
            resources=(str(path),),
            arguments=arguments,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        cancellation.raise_if_cancelled()
        raw_path = _string(arguments, "path")
        old = _string(arguments, "old_string")
        new = _string(arguments, "new_string", allow_empty=True)
        expected = _occurrences(arguments)
        path = context.workspace.resolve(raw_path)
        before = _read_existing(path)
        if before is None:
            raise ToolValidationError(f"File does not exist: {raw_path}")
        found = before.count(old)
        if found != expected:
            raise ToolValidationError(
                f"Expected {expected} occurrence(s) of old_string in {raw_path}, found {found}",
                details={"expected": expected, "found": found},
            )
        cancellation.raise_if_cancelled()
        after = before.replace(old, new)
        _atomic_write(path, after)
        relative = context.workspace.relative(path)
        diff = _unified_diff(before, after, relative)
        evidence = await self._record_change(context, relative, "modified", diff, before, after)
        return ToolResult({**evidence, "replacements": found})


def workspace_mutation_tools(event_bus: EventBus | None = None) -> tuple[Tool, ...]:
    return (EditFileTool(event_bus), WriteFileTool(event_bus))
