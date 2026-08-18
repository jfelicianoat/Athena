"""Canonical workspace boundary used by every filesystem capability."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from uuid import uuid4

from athena.errors import WorkspaceBoundaryError


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    root: Path

    def __post_init__(self) -> None:
        try:
            canonical = self.root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceBoundaryError(f"Workspace root is unavailable: {self.root}") from exc
        if not canonical.is_dir():
            raise WorkspaceBoundaryError(f"Workspace root is not a directory: {canonical}")
        object.__setattr__(self, "root", canonical)

    @classmethod
    def from_path(cls, root: Path | str, workspace_id: str | None = None) -> Workspace:
        return cls(workspace_id or str(uuid4()), Path(root))

    def resolve(self, requested: Path | str, *, must_exist: bool = True) -> Path:
        candidate = Path(requested)
        unresolved = candidate if candidate.is_absolute() else self.root / candidate
        try:
            canonical = unresolved.resolve(strict=must_exist)
        except (FileNotFoundError, OSError) as exc:
            raise WorkspaceBoundaryError(f"Workspace path is unavailable: {requested}") from exc
        if not canonical.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes workspace: {requested}")
        return canonical

    def validate_pattern(self, pattern: str) -> str:
        candidate = PurePath(pattern)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise WorkspaceBoundaryError(f"Pattern escapes workspace: {pattern}")
        return pattern.replace("\\", "/")

    def relative(self, path: Path) -> str:
        canonical = path.resolve(strict=True)
        if not canonical.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes workspace: {path}")
        return canonical.relative_to(self.root).as_posix()
