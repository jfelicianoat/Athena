"""Local checkpoints taken before a high-impact operation.

A checkpoint is a copy of the files an operation is about to touch, kept outside the
workspace so it survives whatever the operation does. It is **not** a commit. Athena does
not write to a user's git history to protect itself: a commit is a public act with a
message, an author and consequences for everyone sharing the branch, and taking one
"just in case" would mean the safety net changes the thing it is protecting.

Restoring is explicit too. Nothing rolls back automatically, because an automatic rollback
would discard work a human might have wanted to inspect.

Layout on disk, one directory per checkpoint::

    <id>/manifest.json   metadata, hashes and attribution
    <id>/files/<path>    the copies

The copies live under `files/` and never next to the manifest. They used to share one
directory, so a user file called `checkpoint.json` was overwritten by the manifest and a
restore put the metadata into the project (A12). Every copy carries the SHA-256 it had
when it was taken; a copy that no longer matches is refused instead of restored.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from athena.errors import ToolExecutionError, WorkspaceBoundaryError
from athena.types import JSONObject
from athena.workspace import Workspace

_MANIFEST = "manifest.json"
#: The manifest name of the layout before A12, still readable.
_LEGACY_MANIFEST = "checkpoint.json"
_FILES = "files"


def file_digest(path: Path) -> str | None:
    """SHA-256 of a regular file, or None when there is no such file."""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    relative_path: str
    #: False when the path did not exist yet, so restoring means deleting it again.
    existed: bool
    size_bytes: int = 0
    #: What the file was when the copy was taken. None when it did not exist.
    before_sha256: str | None = None
    #: What the file was right after Athena wrote it. Set only once the write succeeded;
    #: it is what lets a rollback notice that somebody changed the file afterwards (A11).
    after_sha256: str | None = None
    #: True once the write this copy was taken for actually happened.
    written: bool = False

    def to_json(self) -> JSONObject:
        return {
            "relative_path": self.relative_path,
            "existed": self.existed,
            "size_bytes": self.size_bytes,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "written": self.written,
        }


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    label: str
    workspace_id: str
    entries: tuple[CheckpointEntry, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Which run and task this copy was taken for, so the ability to undo can be rebuilt
    #: after a restart instead of dying with the process (A13).
    run_id: str = ""
    task_id: str = ""
    scope: str = "task"
    covers: tuple[str, ...] = ()
    #: Set once a rollback used this checkpoint, so it is not offered again.
    rolled_back: bool = False

    def to_json(self) -> JSONObject:
        return {
            "checkpoint_id": self.checkpoint_id,
            "label": self.label,
            "workspace_id": self.workspace_id,
            "created_at": self.created_at.isoformat(),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "scope": self.scope,
            "covers": list(self.covers),
            "rolled_back": self.rolled_back,
            "entries": [entry.to_json() for entry in self.entries],
        }


class CheckpointStore:
    """Keeps checkpoints on disk, outside the workspace they protect."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, checkpoint_id: str) -> Path:
        return self.root / checkpoint_id

    def _copy_of(self, checkpoint_id: str, relative: str) -> Path:
        directory = self._directory(checkpoint_id)
        if (directory / _MANIFEST).is_file():
            return directory / _FILES / relative
        # Layout anterior a A12: las copias junto al manifiesto.
        return directory / relative

    def create(
        self,
        workspace: Workspace,
        paths: Iterable[str],
        *,
        label: str = "",
        run_id: str = "",
        task_id: str = "",
        scope: str = "task",
        covers: Sequence[str] = (),
    ) -> Checkpoint:
        """Copy the named paths aside before something changes them."""
        checkpoint_id = str(uuid4())
        directory = self._directory(checkpoint_id)
        (directory / _FILES).mkdir(parents=True, exist_ok=False)
        entries: list[CheckpointEntry] = []
        try:
            for raw in paths:
                # A checkpoint must not be a way to copy files from outside the boundary:
                # `resolve` raises on an escape and the directory is removed below.
                resolved = workspace.resolve(raw, must_exist=False)
                relative = resolved.relative_to(workspace.root).as_posix()
                if resolved.is_file():
                    target = directory / _FILES / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(resolved, target)
                    before = file_digest(resolved)
                    if file_digest(target) != before:
                        raise ToolExecutionError(f"The copy of {relative} is not identical")
                    entries.append(CheckpointEntry(relative, True, resolved.stat().st_size, before))
                elif resolved.exists():
                    raise ToolExecutionError(f"Only regular files can be checkpointed: {relative}")
                else:
                    entries.append(CheckpointEntry(relative, False))
        except (WorkspaceBoundaryError, ToolExecutionError, OSError):
            shutil.rmtree(directory, ignore_errors=True)
            raise
        checkpoint = Checkpoint(
            checkpoint_id,
            label,
            workspace.workspace_id,
            tuple(entries),
            run_id=run_id,
            task_id=task_id,
            scope=scope,
            covers=tuple(covers),
        )
        self._write_manifest(checkpoint)
        return checkpoint

    def _write_manifest(self, checkpoint: Checkpoint) -> None:
        """Atomically: a manifest half written by a crash would lose the whole copy."""
        _atomic_write(
            self._directory(checkpoint.checkpoint_id) / _MANIFEST,
            json.dumps(checkpoint.to_json(), ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def mark_written(
        self, checkpoint: Checkpoint, relative_path: str, after: str | None
    ) -> Checkpoint:
        """Record that the write this copy protects happened, and what it left behind."""
        entries = tuple(
            replace(entry, written=True, after_sha256=after)
            if entry.relative_path == relative_path
            else entry
            for entry in checkpoint.entries
        )
        updated = replace(checkpoint, entries=entries)
        self._write_manifest(updated)
        return updated

    def mark_rolled_back(self, checkpoint: Checkpoint) -> Checkpoint:
        updated = replace(checkpoint, rolled_back=True)
        self._write_manifest(updated)
        return updated

    def list(self) -> tuple[Checkpoint, ...]:
        checkpoints: list[Checkpoint] = []
        for directory in sorted(self.root.iterdir()):
            manifest = directory / _MANIFEST
            if not manifest.is_file():
                manifest = directory / _LEGACY_MANIFEST
            if not manifest.is_file():
                continue
            loaded = self._read(manifest)
            if loaded is not None:
                checkpoints.append(loaded)
        return tuple(sorted(checkpoints, key=lambda item: item.created_at))

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        directory = self._directory(checkpoint_id)
        for name in (_MANIFEST, _LEGACY_MANIFEST):
            manifest = directory / name
            if manifest.is_file():
                return self._read(manifest)
        return None

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
                _text(item.get("before_sha256")),
                _text(item.get("after_sha256")),
                bool(item.get("written", False)),
            )
            for item in (raw_entries if isinstance(raw_entries, Sequence) else ())
            if isinstance(item, dict) and item.get("relative_path")
        )
        try:
            created_at = datetime.fromisoformat(str(payload.get("created_at")))
        except ValueError:
            created_at = datetime.now(UTC)
        covers = payload.get("covers")
        return Checkpoint(
            str(payload.get("checkpoint_id", manifest.parent.name)),
            str(payload.get("label", "")),
            str(payload.get("workspace_id", "")),
            entries,
            created_at,
            run_id=str(payload.get("run_id", "")),
            task_id=str(payload.get("task_id", "")),
            scope=str(payload.get("scope", "task")),
            covers=tuple(str(item) for item in covers) if isinstance(covers, list) else (),
            rolled_back=bool(payload.get("rolled_back", False)),
        )

    def restore_entry(
        self, checkpoint: Checkpoint, entry: CheckpointEntry, workspace: Workspace
    ) -> None:
        """Put one file back as it was, or remove it if it did not exist.

        The copy is checked against the hash it had when it was taken: a damaged copy is
        refused and the file in the project is left exactly as it is.
        """
        target = workspace.resolve(entry.relative_path, must_exist=False)
        if not entry.existed:
            # It did not exist when the checkpoint was taken, so restoring removes it.
            target.unlink(missing_ok=True)
            return
        source = self._copy_of(checkpoint.checkpoint_id, entry.relative_path)
        if not source.is_file():
            raise ToolExecutionError(
                f"Checkpoint {checkpoint.checkpoint_id} is missing {entry.relative_path}"
            )
        if entry.before_sha256 is not None and file_digest(source) != entry.before_sha256:
            raise ToolExecutionError(
                f"The copy of {entry.relative_path} in checkpoint "
                f"{checkpoint.checkpoint_id} is damaged; the project file was not touched"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, source.read_bytes())
        shutil.copystat(source, target)

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
        restored: list[str] = []
        for entry in checkpoint.entries:
            self.restore_entry(checkpoint, entry, workspace)
            restored.append(entry.relative_path)
        return tuple(restored)

    def discard(self, checkpoint_id: str) -> None:
        shutil.rmtree(self._directory(checkpoint_id), ignore_errors=True)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _atomic_write(target: Path, data: bytes) -> None:
    """Write through a sibling temporary file, so a crash never leaves half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


__all__ = ["Checkpoint", "CheckpointEntry", "CheckpointStore", "file_digest"]
