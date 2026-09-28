"""Canonical workspace boundary used by every filesystem capability."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePath

from athena.errors import WorkspaceBoundaryError, WorkspacePathNotFoundError


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
        """El workspace de una carpeta, con una identidad que no cambia entre runs.

        Antes cada llamada inventaba un UUID, asi que dos runs sobre la misma carpeta eran
        dos proyectos distintos para todo lo que se indexa por identidad: la memoria de
        proyecto guardaba lo aprendido bajo un id que nadie volveria a usar, y el replay
        de una peticion idempotente devolvia otro workspace (A08).
        """
        path = Path(root)
        return cls(workspace_id or project_identity(path), path)

    def resolve(self, requested: Path | str, *, must_exist: bool = True) -> Path:
        """Canonicaliza una ruta pedida y responde a dos preguntas en este orden.

        Primero si cruza el limite, y solo despues si existe. El orden es de seguridad:
        una ruta de fuera que ademas no existe se contesta como escape y no como
        «no encontrado», porque lo segundo confirmaria al que pregunta que la ruta esta
        fuera y no ahi — y decir «no existe» de algo de fuera ya es hablar de fuera.

        La distincion importa aguas abajo: `WorkspaceBoundaryError` es un intento de
        cruzar el limite y se aborta; `WorkspacePathNotFoundError` es una errata o un
        nombre inventado y se le cuenta al modelo para que lo corrija.
        """
        candidate = Path(requested)
        unresolved = candidate if candidate.is_absolute() else self.root / candidate
        try:
            # Sin `strict`: la existencia se decide despues, y a proposito. Con
            # `strict=True` el fallo por no existir y el fallo por escapar llegaban aqui
            # como la misma `OSError` y se contestaban con el mismo error de limite.
            canonical = unresolved.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            # Ni resoluble: no hay forma de situarla respecto al limite, asi que se
            # rechaza como limite. Es el lado conservador.
            raise WorkspaceBoundaryError(f"Workspace path is unavailable: {requested}") from exc
        if not canonical.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes workspace: {requested}")
        if must_exist and not canonical.exists():
            raise WorkspacePathNotFoundError(
                f"Path does not exist in the workspace: {requested}",
                details={"path": str(requested)},
            )
        return canonical

    def validate_pattern(self, pattern: str) -> str:
        candidate = PurePath(pattern)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise WorkspaceBoundaryError(f"Pattern escapes workspace: {pattern}")
        return pattern.replace("\\", "/")

    def relative(self, path: Path, *, must_exist: bool = True) -> str:
        # `must_exist=False` para rutas que aun no existen: la copia previa de un fichero
        # que se va a crear necesita su ruta relativa antes de que exista (A10).
        canonical = path.resolve(strict=must_exist)
        if not canonical.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"Path escapes workspace: {path}")
        return canonical.relative_to(self.root).as_posix()


def project_identity(root: Path | str) -> str:
    """Identidad duradera de un proyecto: la de su ruta canonica.

    Canonica de verdad: resuelta (enlaces incluidos) y, en Windows, sin distinguir
    mayusculas, porque `D:\\Repo` y `d:\\repo` son la misma carpeta. Mover el proyecto
    de sitio le da otra identidad; es lo esperable de algo que se identifica por donde
    esta, y se documenta asi en vez de adivinar que dos carpetas son la misma.
    """
    try:
        canonical = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        canonical = Path(root).expanduser().absolute()
    key = os.path.normcase(str(canonical))
    return "proj-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
