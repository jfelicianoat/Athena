"""Verificacion antes de completar: nada se da por hecho porque lo diga el modelo.

Partido por responsabilidad:

- `contratos`    — comprobaciones, evidencia, linea base y resultado.
- `plan`         — de donde salen las comprobaciones del proyecto.
- `integridad`   — que el diff no traiga lo que nadie autorizo.
- `politicas`    — las politicas que autorizan (o no) completar.
- `artefactos`   — comprobacion de que existe lo prometido.
"""

from __future__ import annotations

from athena.verification.artefactos import ArtifactVerificationPolicy
from athena.verification.contratos import (
    Baseline,
    CheckKind,
    CheckOutcome,
    PlanSource,
    VerificationCheck,
    VerificationEvidence,
    VerificationPlan,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from athena.verification.integridad import (
    ChangeIntegrityPolicy,
    IntegrityAuthorization,
    IntegrityFinding,
)
from athena.verification.plan import VerificationPlanner
from athena.verification.politicas import (
    CommandVerificationPolicy,
    LoopCompletionVerificationPolicy,
    evidence_digest,
)

__all__ = [
    "ArtifactVerificationPolicy",
    "Baseline",
    "ChangeIntegrityPolicy",
    "CheckKind",
    "CheckOutcome",
    "CommandVerificationPolicy",
    "IntegrityAuthorization",
    "IntegrityFinding",
    "LoopCompletionVerificationPolicy",
    "PlanSource",
    "VerificationCheck",
    "VerificationEvidence",
    "VerificationPlan",
    "VerificationPlanner",
    "VerificationPolicy",
    "VerificationResult",
    "VerificationStatus",
    "evidence_digest",
]
