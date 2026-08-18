"""Dynamic, bounded context construction for repository investigation."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from athena.async_utils import await_cancellable
from athena.cancellation import CancellationToken
from athena.errors import WorkspaceBoundaryError
from athena.models import ModelMessage, ModelRequest, ModelRole
from athena.types import JSONObject
from athena.workspace import Workspace


@dataclass(frozen=True, slots=True)
class ContextLimits:
    git_status_chars: int = 4_000
    recent_log_entries: int = 5
    instructions_chars: int = 16_000
    git_timeout_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class ProjectContext:
    workspace_root: str
    git: JSONObject = field(default_factory=dict)
    instructions: tuple[tuple[str, str], ...] = ()


class ContextBuilder:
    """Builds model context without copying the repository into the prompt."""

    def __init__(self, workspace: Workspace, *, limits: ContextLimits | None = None) -> None:
        self.workspace = workspace
        self.limits = limits or ContextLimits()

    async def inspect_project(
        self,
        cancellation: CancellationToken,
        discovered_paths: tuple[str, ...] = (),
    ) -> ProjectContext:
        git = await await_cancellable(
            asyncio.to_thread(self._git_context),
            cancellation,
            timeout=self.limits.git_timeout_seconds * 5,
        )
        instructions = self.resolve_agent_instructions(discovered_paths)
        cancellation.raise_if_cancelled()
        return ProjectContext(str(self.workspace.root), git, instructions)

    async def build_request(
        self,
        *,
        objective: str,
        history: tuple[ModelMessage, ...],
        important_state: JSONObject,
        tool_definitions: tuple[JSONObject, ...],
        cancellation: CancellationToken,
        discovered_paths: tuple[str, ...] = (),
    ) -> ModelRequest:
        project = await self.inspect_project(cancellation, discovered_paths)
        system = self._render(project, important_state)
        messages = (
            ModelMessage(ModelRole.SYSTEM, system),
            ModelMessage(ModelRole.USER, objective),
            *history,
        )
        return ModelRequest(messages=messages, tools=tool_definitions)

    def resolve_agent_instructions(
        self, discovered_paths: tuple[str, ...] = ()
    ) -> tuple[tuple[str, str], ...]:
        candidates: list[Path] = [self.workspace.root / "AGENTS.md"]
        for raw_path in discovered_paths:
            try:
                resolved = self.workspace.resolve(raw_path)
            except WorkspaceBoundaryError:
                continue
            current = resolved if resolved.is_dir() else resolved.parent
            directories: list[Path] = []
            while current.is_relative_to(self.workspace.root):
                directories.append(current)
                if current == self.workspace.root:
                    break
                current = current.parent
            candidates.extend(directory / "AGENTS.md" for directory in reversed(directories))
        seen: set[Path] = set()
        instructions: list[tuple[str, str]] = []
        remaining = self.limits.instructions_chars
        for candidate in candidates:
            if remaining <= 0:
                continue
            try:
                canonical = self.workspace.resolve(candidate)
            except WorkspaceBoundaryError:
                continue
            if canonical in seen or not canonical.is_file():
                continue
            seen.add(canonical)
            content = canonical.read_text(encoding="utf-8")[:remaining]
            remaining -= len(content)
            instructions.append((self.workspace.relative(canonical), content))
        return tuple(instructions)

    def _git_context(self) -> JSONObject:
        try:
            git_marker = self.workspace.resolve(".git")
        except WorkspaceBoundaryError:
            return {}
        if not git_marker.is_dir():
            return {}
        top_level = self._git("rev-parse", "--show-toplevel")
        if not top_level:
            return {}
        try:
            if Path(top_level).resolve(strict=True) != self.workspace.root:
                return {}
        except OSError:
            return {}
        branch = self._git("branch", "--show-current")
        remote_default = self._git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        default_branch = remote_default.removeprefix("origin/")
        if not remote_default and branch in ("main", "master"):
            default_branch = branch
        elif not default_branch:
            default_branch = self._git("config", "--get", "init.defaultBranch")
        raw_status = self._git("status", "--short")
        status = self._bounded(raw_status, self.limits.git_status_chars)
        log = self._git(
            "log",
            f"-{self.limits.recent_log_entries}",
            "--pretty=format:%h %s",
        )
        return {
            "branch": branch or None,
            "default_branch": default_branch or None,
            "status": status,
            "recent_log": log.splitlines() if log else [],
            "status_truncated": len(raw_status) > self.limits.git_status_chars,
        }

    def _git(self, *arguments: str) -> str:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={self.workspace.root}",
                    "-C",
                    str(self.workspace.root),
                    *arguments,
                ],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                timeout=self.limits.git_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    @staticmethod
    def _bounded(value: str, maximum: int) -> str:
        if len(value) <= maximum:
            return value
        return value[:maximum] + "\n…[truncated]"

    @staticmethod
    def _render(project: ProjectContext, important_state: JSONObject) -> str:
        instruction_text = "\n\n".join(
            f"[{path}]\n{content}" for path, content in project.instructions
        )
        return (
            "You are Athena, investigating a repository with read-only tools. "
            "Never claim to modify files. Prefer Glob → Grep → ReadRange; use ReadFile only "
            "for a known small file. Finish with a non-empty answer and no tool calls.\n\n"
            f"Workspace root: {project.workspace_root}\n"
            f"Git context: {json.dumps(project.git, ensure_ascii=False)}\n"
            f"Important session state: {json.dumps(important_state, ensure_ascii=False)}\n"
            "Project instructions, root first and more specific last:\n"
            f"{instruction_text or '(none)'}"
        )
