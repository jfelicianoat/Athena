"""Finite runtime budget accounting prepared for future budget dimensions."""

from __future__ import annotations

from dataclasses import dataclass

from athena.errors import BudgetExceededError


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_iterations: int
    max_model_calls: int | None = None
    max_tool_calls: int | None = None


@dataclass(slots=True)
class BudgetUsage:
    iterations: int = 0
    model_calls: int = 0
    tool_calls: int = 0


class RuntimeBudget:
    def __init__(self, limits: BudgetLimits) -> None:
        if limits.max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        self.limits = limits
        self.usage = BudgetUsage()

    def consume_iteration(self) -> None:
        self.usage.iterations += 1
        if self.usage.iterations > self.limits.max_iterations:
            raise BudgetExceededError("Maximum agent iterations exceeded")

    def consume_model_call(self) -> None:
        self.usage.model_calls += 1
        if (
            self.limits.max_model_calls is not None
            and self.usage.model_calls > self.limits.max_model_calls
        ):
            raise BudgetExceededError("Maximum model calls exceeded")

    def consume_tool_call(self) -> None:
        self.usage.tool_calls += 1
        if (
            self.limits.max_tool_calls is not None
            and self.usage.tool_calls > self.limits.max_tool_calls
        ):
            raise BudgetExceededError("Maximum tool calls exceeded")
