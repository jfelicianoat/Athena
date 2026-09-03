"""Runs vivos del servicio: opciones, suscripciones y registro.

- `opciones`     — lo que pide quien arranca un run.
- `suscripcion`  — runs vivos y quien los mira.
- `registro`     — arranque, reanudacion, difusion y control.
"""

from __future__ import annotations

from athena.adapters.service.runs.opciones import CapabilityMode, RunOptions
from athena.adapters.service.runs.registro import RunRegistry, build_workspace
from athena.adapters.service.runs.suscripcion import LiveRun, Subscriber

__all__ = [
    "CapabilityMode",
    "LiveRun",
    "RunOptions",
    "RunRegistry",
    "Subscriber",
    "build_workspace",
]
