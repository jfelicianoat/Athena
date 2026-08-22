"""Git capabilities.

Read operations (status, diff, log, show) are R0 and resolve to ALLOW. Recording a
commit is R3 and always resolves to ASK. There is deliberately no push, pull, fetch,
merge, rebase, tag-move, pull-request or deploy tool: remote and irreversible actions
are not part of Athena's capability surface, so the model cannot request one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from athena.cancellation import CancellationToken
from athena.errors import ToolExecutionError, ToolValidationError
from athena.permissions import PermissionRequest, RiskLevel, RiskTier
from athena.process_tools import run_process
from athena.tools import Tool, ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject

_GIT_TIMEOUT_SECONDS = 20.0
_MAX_GIT_OUTPUT_CHARS = 16_000
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^-]{0,99}$")


def _bounded(value: str) -> tuple[str, bool]:
    if len(value) <= _MAX_GIT_OUTPUT_CHARS:
        return value, False
    return value[:_MAX_GIT_OUTPUT_CHARS] + "\n...[truncated]", True


def _integer(arguments: JSONObject, name: str, *, default: int, maximum: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolValidationError(f"{name} must be an integer")
    if value < 1 or value > maximum:
        raise ToolValidationError(f"{name} must be between 1 and {maximum}")
    return value


def _flag(arguments: JSONObject, name: str) -> bool:
    value = arguments.get(name, False)
    if not isinstance(value, bool):
        raise ToolValidationError(f"{name} must be a boolean")
    return value


def _revision(arguments: JSONObject, name: str, *, default: str) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str) or not value:
        raise ToolValidationError(f"{name} must be a non-empty string")
    if not _REVISION.match(value):
        raise ToolValidationError(f"{name} is not a valid revision: {value}")
    return value


def _relative_paths(context: ToolContext, arguments: JSONObject) -> tuple[str, ...]:
    raw = arguments.get("paths", ())
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ToolValidationError("paths must be a list of workspace-relative strings")
    resolved: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ToolValidationError("every entry in paths must be a non-empty string")
        canonical = context.workspace.resolve(item)
        resolved.append(context.workspace.relative(canonical))
    return tuple(resolved)


class _GitTool:
    """Shared git invocation confined to the workspace root."""

    spec: ClassVar[ToolSpec]

    async def _git(
        self,
        context: ToolContext,
        arguments: tuple[str, ...],
        cancellation: CancellationToken,
    ) -> tuple[int, str, str]:
        root = context.workspace.root
        if not (root / ".git").exists():
            raise ToolValidationError("The workspace is not a git repository")
        argv = ("git", "-c", f"safe.directory={root}", "-C", str(root), *arguments)
        try:
            return await run_process(
                argv,
                cwd=root,
                timeout_seconds=_GIT_TIMEOUT_SECONDS,
                cancellation=cancellation,
            )
        except ToolExecutionError as exc:
            raise ToolExecutionError("git is not available on this system") from exc

    @staticmethod
    def _workspace_root(context: ToolContext) -> Path:
        return context.workspace.root


class _GitReadTool(_GitTool):
    def is_read_only(self, arguments: JSONObject) -> bool:
        del arguments
        return True

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        del arguments
        return True

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        return PermissionRequest(
            tool_name=self.spec.name,
            operation=self.spec.name,
            action=f"inspect git {self.spec.name.removeprefix('git_')}",
            workspace=context.workspace,
            risk=RiskLevel.LOW,
            tier=RiskTier.R0_READ_ONLY,
            is_read_only=True,
            is_destructive=False,
            is_concurrency_safe=True,
            reason="The agent requested read-only git history or working-tree state.",
            possible_effects=("Reads local git state", "Changes nothing"),
            resources=(str(context.workspace.root),),
            arguments=arguments,
        )


class GitStatusTool(_GitReadTool):
    spec = ToolSpec(
        name="git_status",
        description="Show the working tree status in porcelain form.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "clean": {"type": "boolean"},
                "changed_files": {"type": "integer"},
                "status": {"type": "string"},
                "truncated": {"type": "boolean"},
            },
            "required": ["clean", "changed_files", "status", "truncated"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=12_000,
        search_hint="check whether the workspace is dirty",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        if set(arguments):
            raise ToolValidationError("git_status takes no arguments")
        return {}

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        del arguments
        _, stdout, _ = await self._git(context, ("status", "--porcelain"), cancellation)
        _, branch, _ = await self._git(context, ("branch", "--show-current"), cancellation)
        entries = [line for line in stdout.splitlines() if line.strip()]
        text, truncated = _bounded("\n".join(entries))
        return ToolResult(
            {
                "branch": branch.strip() or None,
                "clean": not entries,
                "changed_files": len(entries),
                "status": text,
                "truncated": truncated,
            }
        )


class GitDiffTool(_GitReadTool):
    spec = ToolSpec(
        name="git_diff",
        description="Show the unified diff of unstaged or staged changes.",
        input_schema={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "default": False},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "diff": {"type": "string"},
                "empty": {"type": "boolean"},
                "truncated": {"type": "boolean"},
            },
            "required": ["staged", "paths", "diff", "empty", "truncated"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=16_000,
        search_hint="review what changed before reporting completion",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        unknown = set(arguments) - {"staged", "paths"}
        if unknown:
            raise ToolValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")
        raw = arguments.get("paths", ())
        if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
            raise ToolValidationError("paths must be a list of workspace-relative strings")
        paths = [item for item in raw if isinstance(item, str)]
        if len(paths) != len(raw):
            raise ToolValidationError("every entry in paths must be a string")
        return {"staged": _flag(arguments, "staged"), "paths": paths}

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        staged = _flag(arguments, "staged")
        paths = _relative_paths(context, arguments)
        command = ["diff", "--stat", "--patch"]
        if staged:
            command.append("--cached")
        if paths:
            command.append("--")
            command.extend(paths)
        _, stdout, _ = await self._git(context, tuple(command), cancellation)
        text, truncated = _bounded(stdout)
        return ToolResult(
            {
                "staged": staged,
                "paths": list(paths),
                "diff": text,
                "empty": not stdout.strip(),
                "truncated": truncated,
            }
        )


class GitLogTool(_GitReadTool):
    spec = ToolSpec(
        name="git_log",
        description="Show recent commits as compact one-line entries.",
        input_schema={
            "type": "object",
            "properties": {"max_entries": {"type": "integer", "minimum": 1, "maximum": 100}},
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "entries": {"type": "array", "items": {"type": "string"}},
                "max_entries": {"type": "integer"},
            },
            "required": ["entries", "max_entries"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=12_000,
        search_hint="understand recent history",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        unknown = set(arguments) - {"max_entries"}
        if unknown:
            raise ToolValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")
        return {"max_entries": _integer(arguments, "max_entries", default=10, maximum=100)}

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        maximum = _integer(arguments, "max_entries", default=10, maximum=100)
        _, stdout, _ = await self._git(
            context, ("log", f"-{maximum}", "--pretty=format:%h %an %s"), cancellation
        )
        return ToolResult({"entries": stdout.splitlines(), "max_entries": maximum})


class GitShowTool(_GitReadTool):
    spec = ToolSpec(
        name="git_show",
        description="Show one commit, including its message and patch.",
        input_schema={
            "type": "object",
            "properties": {"revision": {"type": "string", "default": "HEAD"}},
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "revision": {"type": "string"},
                "content": {"type": "string"},
                "truncated": {"type": "boolean"},
            },
            "required": ["revision", "content", "truncated"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=16_000,
        search_hint="inspect one specific commit",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        unknown = set(arguments) - {"revision"}
        if unknown:
            raise ToolValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")
        return {"revision": _revision(arguments, "revision", default="HEAD")}

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        revision = _revision(arguments, "revision", default="HEAD")
        exit_code, stdout, stderr = await self._git(
            context, ("show", "--stat", "--patch", revision, "--"), cancellation
        )
        if exit_code != 0:
            raise ToolValidationError(
                f"Unknown revision: {revision}", details={"stderr": stderr[:500]}
            )
        text, truncated = _bounded(stdout)
        return ToolResult({"revision": revision, "content": text, "truncated": truncated})


class GitCommitTool(_GitTool):
    """Recording a commit is irreversible enough to always require a human ASK."""

    spec = ToolSpec(
        name="git_commit",
        description=(
            "Stage the given workspace paths and record one local commit. "
            "Athena cannot push, merge or otherwise publish it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["message", "paths"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "committed": {"type": "boolean"},
                "revision": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "output": {"type": "string"},
            },
            "required": ["committed", "revision", "paths"],
            "additionalProperties": False,
        },
        risk=RiskLevel.HIGH,
        max_result_size_chars=8_000,
        search_hint="record work that the user explicitly asked to commit",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        unknown = set(arguments) - {"message", "paths"}
        if unknown:
            raise ToolValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ToolValidationError("message must be a non-empty string")
        paths = arguments.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            raise ToolValidationError("paths must list at least one workspace-relative path")
        return {"message": message, "paths": list(paths)}

    def is_read_only(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        paths = _relative_paths(context, arguments)
        message = str(arguments.get("message", "")).splitlines()[:1]
        return PermissionRequest(
            tool_name=self.spec.name,
            operation="git_commit",
            action=f"commit {len(paths)} path(s): {message[0] if message else ''}",
            workspace=context.workspace,
            risk=RiskLevel.HIGH,
            tier=RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE,
            is_read_only=False,
            is_destructive=False,
            is_concurrency_safe=False,
            reason="Recording a commit changes history that Athena cannot undo on its own.",
            possible_effects=(
                f"Stages and commits: {', '.join(paths)}",
                "Writes a new entry into local git history",
                "Does not push, merge or publish anything",
            ),
            resources=tuple(paths),
            arguments=arguments,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        paths = _relative_paths(context, arguments)
        message = str(arguments.get("message", ""))
        add_code, _, add_error = await self._git(context, ("add", "--", *paths), cancellation)
        if add_code != 0:
            raise ToolExecutionError("git add failed", details={"stderr": add_error[:500]})
        commit_code, stdout, stderr = await self._git(
            context, ("commit", "-m", message, "--", *paths), cancellation
        )
        if commit_code != 0:
            raise ToolExecutionError(
                "git commit failed", details={"stderr": stderr[:500], "stdout": stdout[:500]}
            )
        _, revision, _ = await self._git(context, ("rev-parse", "HEAD"), cancellation)
        return ToolResult(
            {
                "committed": True,
                "revision": revision.strip(),
                "paths": list(paths),
                "output": stdout.strip()[:2_000],
            }
        )


def git_read_tools() -> tuple[Tool, ...]:
    return (GitStatusTool(), GitDiffTool(), GitLogTool(), GitShowTool())
