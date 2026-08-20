"""Local checkpoints taken before a high-impact operation.

A checkpoint is a copy of the files an operation is about to touch, kept outside the
workspace so it survives whatever the operation does. It is **not** a commit. Athena does
not write to a user's git history to protect itself: a commit is a public act with a
message, an author and consequences for everyone sharing the branch, and taking one
"just in case" would mean the safety net changes the thing it is protecting.

Restoring is explicit too. Nothing rolls back automatically, because an automatic rollback
would discard work a human might have wanted to inspect.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from athena.errors import ToolExecutionError, WorkspaceBoundaryError
from athena.types import JSONObject
from athena.workspace import Workspace

_MANIFEST = "checkpoint.json"


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    relative_path: str
    #: False when the path did not exist yet, so restoring means deleting it again.
    existed: bool
    size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    label: str
    workspace_id: str
    entries: tuple[CheckpointEntry, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> JSONObject:
        return {
            "checkpoint_id": self.checkpoint_id,
            "label": self.label,
            "workspace_id": self.workspace_id,
            "created_at": self.created_at.isoformat(),
            "entries": [
                {
                    "relative_path": entry.relative_path,
                    "existed": entry.existed,
                    "size_bytes": entry.size_bytes,
                }
                for entry in self.entries
            ],
        }


class CheckpointStore:
    """Keeps checkpoints on disk, outside the workspace they protect."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, checkpoint_id: str) -> Path:
        return self.root / checkpoint_id

    def create(self, workspace: Workspace, paths: Iterable[str], *, label: str = "") -> Checkpoint:
        """Copy the named paths aside before something changes them."""
        checkpoint_id = str(uuid4())
        directory = self._directory(checkpoint_id)
        directory.mkdir(parents=True, exist_ok=False)
        entries: list[CheckpointEntry] = []
        for raw in paths:
            try:
                resolved = workspace.resolve(raw, must_exist=False)
            except WorkspaceBoundaryError:
                # A checkpoint must not be a way to copy files from outside the boundary.
                raise
            relative = resolved.relative_to(workspace.root).as_posix()
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if resolved.is_file():
                shutil.copy2(resolved, target)
                entries.append(CheckpointEntry(relative, True, resolved.stat().st_size))
            elif resolved.exists():
                raise ToolExecutionError(f"Only regular files can be checkpointed: {relative}")
            else:
                entries.append(CheckpointEntry(relative, False))
        checkpoint = Checkpoint(checkpoint_id, label, workspace.workspace_id, tuple(entries))
        (directory / _MANIFEST).write_text(
            json.dumps(checkpoint.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return checkpoint

    def list(self) -> tuple[Checkpoint, ...]:
        checkpoints: list[Checkpoint] = []
        for directory in sorted(self.root.iterdir()):
            manifest = directory / _MANIFEST
            if not manifest.is_file():
                continue
            loaded = self._read(manifest)
            if loaded is not None:
                checkpoints.append(loaded)
        return tuple(checkpoints)

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        manifest = self._directory(checkpoint_id) / _MANIFEST
        return self._read(manifest) if manifest.is_file() else None

    @staticmethod
    def _read(manifest: Path) -> Checkpoint | None:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        raw_entries = payload.get("entries")
        entries = tuple(
            CheckpointEntry(
                str(item.get("relative_path", "")),
                bool(item.get("existed")),
                int(item.get("size_bytes", 0)),
            )
            for item in (raw_entries if isinstance(raw_entries, Sequence) else ())
            if isinstance(item, dict) and item.get("relative_path")
        )
        try:
            created_at = datetime.fromisoformat(str(payload.get("created_at")))
        except ValueError:
            created_at = datetime.now(UTC)
        return Checkpoint(
            str(payload.get("checkpoint_id", manifest.parent.name)),
            str(payload.get("label", "")),
            str(payload.get("workspace_id", "")),
            entries,
            created_at,
        )

    def restore(self, checkpoint: Checkpoint, workspace: Workspace) -> tuple[str, ...]:
        """Put the files back. Only ever called deliberately."""
        if checkpoint.workspace_id != workspace.workspace_id:
            raise ToolExecutionError(
                "Refusing to restore a checkpoint taken in a different workspace",
                details={
                    "checkpoint_workspace": checkpoint.workspace_id,
                    "workspace": workspace.workspace_id,
                },
            )
        directory = self._directory(checkpoint.checkpoint_id)
        restored: list[str] = []
        for entry in checkpoint.entries:
            target = workspace.resolve(entry.relative_path, must_exist=False)
            source = directory / entry.relative_path
            if entry.existed:
                if not source.is_file():
                    raise ToolExecutionError(
                        f"Checkpoint {checkpoint.checkpoint_id} is missing {entry.relative_path}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                # It did not exist when the checkpoint was taken, so restoring removes it.
                target.unlink(missing_ok=True)
            restored.append(entry.relative_path)
        return tuple(restored)

    def discard(self, checkpoint_id: str) -> None:
        shutil.rmtree(self._directory(checkpoint_id), ignore_errors=True)


__all__ = ["Checkpoint", "CheckpointEntry", "CheckpointStore"]
