"""El registro de runs: compone construccion y ciclo de vida."""

from __future__ import annotations

from athena.adapters.service.runs.ciclo import CicloMixin
from athena.adapters.service.runs.construccion import build_workspace

__all__ = ["RunRegistry", "build_workspace"]


class RunRegistry(CicloMixin):
    """Duenno de los runs vivos: quien manda sobre cada uno y que recibe."""
