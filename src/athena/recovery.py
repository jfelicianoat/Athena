"""Explicit recovery policy, one directive per typed error.

There is deliberately no `except Exception: retry`. An error Athena has not classified
is not retried; it aborts the run, because retrying an unknown failure is how a runtime
turns one bug into an expensive loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from athena.errors import (
    AthenaRuntimeError,
    BudgetExceededError,
    ContextOverflowError,
    FatalRuntimeError,
    ModelPermanentError,
    ModelTransientError,
    PermissionDeniedError,
    ProcessTimeoutError,
    ToolExecutionError,
    ToolValidationError,
    VerificationFailure,
    WorkspaceBoundaryError,
)
from athena.state import ExecutionOutcome, classify_outcome


class RecoveryAction(StrEnum):
    """What the runtime does next. The model never chooses this."""

    #: Hand a structured error back to the model so it can change its input.
    INFORM_MODEL = "inform_model"
    #: Retry the same operation after a backoff.
    RETRY_BACKOFF = "retry_backoff"
    #: Retry a bounded number of times, then tell the model to change strategy.
    LIMITED_RETRY = "limited_retry"
    #: Shrink the context and try again.
    COMPACT_CONTEXT = "compact_context"
    #: Return verification evidence to the loop for a repair cycle.
    RETURN_EVIDENCE = "return_evidence"
    #: Stop this action, report it, and never retry it automatically.
    NO_RETRY = "no_retry"
    #: Abandon the action and fail the run.
    ABORT = "abort"
    #: End the run without a failure verdict.
    STOP = "stop"
    #: The run was cancelled.
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RecoveryDirective:
    action: RecoveryAction
    reason: str
    max_attempts: int = 0
    backoff_seconds: float = 0.0

    @property
    def retries(self) -> bool:
        return self.action in (
            RecoveryAction.RETRY_BACKOFF,
            RecoveryAction.LIMITED_RETRY,
            RecoveryAction.COMPACT_CONTEXT,
        )

    @property
    def ends_run(self) -> bool:
        return self.action in (
            RecoveryAction.ABORT,
            RecoveryAction.STOP,
            RecoveryAction.CANCELLED,
        )


@dataclass(frozen=True, slots=True)
class RecoveryLimits:
    model_retries: int = 2
    model_backoff_seconds: float = 0.1
    process_retries: int = 1
    context_compactions: int = 1


class RecoveryPolicy:
    """Maps a typed error to the single action the runtime is allowed to take."""

    def __init__(
        self,
        limits: RecoveryLimits | None = None,
        *,
        provider_fallback: bool = False,
    ) -> None:
        self.limits = limits or RecoveryLimits()
        self.provider_fallback = provider_fallback

    def decide(self, error: AthenaRuntimeError) -> RecoveryDirective:
        # Being stopped outranks whatever the failing operation was, and what counts as
        # "stopped" is decided in one place so this cannot drift from the loop's answer.
        outcome = classify_outcome(error)
        if outcome.is_stopped_deliberately:
            wording = (
                "The session ran out of time."
                if outcome is ExecutionOutcome.TIMED_OUT
                else "The session was cancelled."
            )
            return RecoveryDirective(RecoveryAction.CANCELLED, wording)
        if isinstance(error, WorkspaceBoundaryError):
            return RecoveryDirective(
                RecoveryAction.ABORT,
                "The action left the workspace boundary and is abandoned.",
            )
        if isinstance(error, PermissionDeniedError):
            return RecoveryDirective(
                RecoveryAction.NO_RETRY,
                "Permission was refused; repeating the request would only re-ask.",
            )
        if isinstance(error, ToolValidationError):
            return RecoveryDirective(
                RecoveryAction.INFORM_MODEL,
                "The tool input was invalid; the model must correct it.",
            )
        if isinstance(error, ProcessTimeoutError):
            return RecoveryDirective(
                RecoveryAction.LIMITED_RETRY,
                "The command timed out; retry briefly, then change strategy.",
                max_attempts=self.limits.process_retries,
            )
        if isinstance(error, ContextOverflowError):
            return RecoveryDirective(
                RecoveryAction.COMPACT_CONTEXT,
                "The context is too large; compact it and retry.",
                max_attempts=self.limits.context_compactions,
            )
        if isinstance(error, ModelTransientError):
            return RecoveryDirective(
                RecoveryAction.RETRY_BACKOFF,
                "The provider failed transiently; retry with a backoff.",
                max_attempts=self.limits.model_retries,
                backoff_seconds=self.limits.model_backoff_seconds,
            )
        if isinstance(error, ModelPermanentError):
            if self.provider_fallback:
                return RecoveryDirective(
                    RecoveryAction.LIMITED_RETRY,
                    "The provider failed permanently; fall back to the next provider.",
                    max_attempts=1,
                )
            return RecoveryDirective(
                RecoveryAction.ABORT,
                "The provider failed permanently and no fallback is configured.",
            )
        if isinstance(error, VerificationFailure):
            return RecoveryDirective(
                RecoveryAction.RETURN_EVIDENCE,
                "Verification failed; return the evidence for a repair cycle.",
            )
        if isinstance(error, BudgetExceededError):
            return RecoveryDirective(RecoveryAction.STOP, "The run exhausted its budget and stops.")
        if isinstance(error, FatalRuntimeError):
            return RecoveryDirective(
                RecoveryAction.ABORT, "An unrecoverable runtime failure aborts the run."
            )
        if isinstance(error, ToolExecutionError):
            return RecoveryDirective(
                RecoveryAction.INFORM_MODEL,
                "The tool failed to execute; report it and let the model adapt.",
            )
        return RecoveryDirective(
            RecoveryAction.ABORT,
            f"{type(error).__name__} has no recovery policy, so it is not retried.",
        )
