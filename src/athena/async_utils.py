"""Cancellation-aware helpers for model and tool operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
from typing import TypeVar

from athena.cancellation import CancellationToken
from athena.errors import CancellationError, ProcessTimeoutError

T = TypeVar("T")


async def await_cancellable(
    operation: Awaitable[T],
    cancellation: CancellationToken,
    *,
    timeout: float | None = None,
) -> T:
    cancellation.raise_if_cancelled()
    operation_task = asyncio.ensure_future(operation)
    cancellation_task = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            {operation_task, cancellation_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done:
            operation_task.cancel()
            with suppress(asyncio.CancelledError):
                await operation_task
            raise CancellationError("Operation was cancelled")
        if operation_task not in done:
            operation_task.cancel()
            with suppress(asyncio.CancelledError):
                await operation_task
            raise ProcessTimeoutError("Operation timed out")
        return await operation_task
    except asyncio.CancelledError:
        operation_task.cancel()
        with suppress(asyncio.CancelledError):
            await operation_task
        raise
    finally:
        cancellation_task.cancel()
        with suppress(asyncio.CancelledError):
            await cancellation_task
