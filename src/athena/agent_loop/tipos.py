"""Configuracion, resultado y estado vivo de una ejecucion del bucle."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from athena.errors import (
    AthenaRuntimeError,
)
from athena.goals import GoalBoard
from athena.memory import CompactionReport
from athena.models import (
    ModelMessage,
)
from athena.progress import NoProgressDetector
from athena.session_store import (
    EventCheckpoint,
)
from athena.skills import SkillSelection
from athena.state import (
    SessionState,
)
from athena.verification import (
    VerificationResult,
)
from athena.working_state import WorkingState


class AgentRunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    max_iterations: int = 12
    session_timeout_seconds: float = 120.0
    max_model_retries: int = 2
    retry_backoff_seconds: float = 0.1
    max_tool_calls: int = 100
    max_repair_cycles: int = 2
    capture_baseline: bool = True
    require_workspace_change: bool = False
    #: El modelo con el que corre este run. Vacio = el que decida el proveedor. El bucle
    #: no valida el nombre: quien despliega decide cuales existen (`ModelCatalog`), y el
    #: bucle solo transmite la eleccion ya tomada.
    model: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    #: The client's declaration that a review was or was not requested. `None` means it
    #: said nothing, and the objective's wording decides (`system1.review_requested`).
    mandatory_review: bool | None = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: AgentRunStatus
    session: SessionState
    answer: str | None = None
    error: AthenaRuntimeError | None = None
    tool_call_ids: tuple[str, ...] = ()
    verification: VerificationResult | None = None
    working_state: WorkingState | None = None


@dataclass(slots=True)
class _RunData:
    session: SessionState
    working: WorkingState
    history: list[ModelMessage] = field(default_factory=list)
    seen_call_ids: set[str] = field(default_factory=set)
    discovered_paths: set[str] = field(default_factory=set)
    repair_cycles: int = 0
    compactions: int = 0
    last_verification: VerificationResult | None = None
    references: list[object] = field(default_factory=list)
    checkpoints: list[EventCheckpoint] = field(default_factory=list)
    pending_compaction: CompactionReport | None = None
    revealed_tools: set[str] = field(default_factory=set)
    skills: tuple[SkillSelection, ...] = ()
    goal: GoalBoard = field(default_factory=lambda: GoalBoard("(sin objetivo)"))
    progress: NoProgressDetector = field(default_factory=NoProgressDetector)
    review_required: bool = False
    latest_output: str = ""
    #: Only `latest_output` was cut short. It matters to the reviewer gate, which judges
    #: that text, and is recomputed on every call: a large file read earlier in the run
    #: says nothing about whether the latest result can be judged in full.
    latest_output_truncated: bool = False
