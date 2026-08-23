"""Bounded worker boundary for synchronous optimization and providers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

from domoai.domain.models import ErrorDetail
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import OptimizationScenario

T = TypeVar("T")


@dataclass(frozen=True)
class WorkerBudget:
    """Deployment-owned limits; caller scenarios can only reduce them."""

    max_solver_time_seconds: float = 30.0
    queue_capacity: int = 2
    max_concurrency: int = 1
    queue_wait_seconds: float = 0.25
    provider_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.max_solver_time_seconds <= 0:
            raise ValueError("max_solver_time_seconds must be positive")
        if self.queue_capacity < 0:
            raise ValueError("queue_capacity must not be negative")
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.queue_wait_seconds <= 0 or self.provider_timeout_seconds <= 0:
            raise ValueError("worker timeouts must be positive")


class WorkerOperationError(RuntimeError):
    """Typed failure for a synchronous operation crossing the worker boundary."""

    def __init__(self, code: str, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.cause = cause


class OptimizationWorker:
    """Run blocking work with finite admission and no event-loop blocking."""

    def __init__(self, service: Any, budget: WorkerBudget | None = None) -> None:
        self.service = service
        self.budget = budget or WorkerBudget()
        self._executor = ThreadPoolExecutor(max_workers=self.budget.max_concurrency)
        self._capacity = asyncio.Semaphore(
            self.budget.max_concurrency + self.budget.queue_capacity
        )
        self.last_queue_wait_seconds: float | None = None
        self.last_wall_time_seconds: float | None = None

    async def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        requested = scenario.solver_time_limit_seconds
        effective = min(requested, self.budget.max_solver_time_seconds)
        capped = scenario.model_copy(update={"solver_time_limit_seconds": effective})
        try:
            result = await self.run_blocking(
                self.service.optimize,
                capped,
                timeout=max(effective, 0.05),
            )
        except WorkerOperationError as error:
            status = (
                OptimizationStatus.TIMEOUT
                if error.code == "worker_timeout"
                else OptimizationStatus.UNKNOWN
            )
            return OptimizationResult(
                scenario_id=scenario.id,
                status=status,
                solver="bounded-worker",
                diagnostics=[
                    ErrorDetail(
                        code=error.code,
                        message=error.message,
                        retryable=error.code in {"worker_timeout", "worker_queue_full"},
                        details={
                            "requested_solver_time_seconds": requested,
                            "effective_solver_time_seconds": effective,
                        },
                    )
                ],
            )
        if not isinstance(result, OptimizationResult):
            return OptimizationResult(
                scenario_id=scenario.id,
                status=OptimizationStatus.UNKNOWN,
                solver="bounded-worker",
                diagnostics=[
                    ErrorDetail(
                        code="worker_invalid_result",
                        message="Optimization worker returned an invalid result",
                    )
                ],
            )
        return result

    async def run_blocking(
        self,
        operation: Callable[..., T],
        *args: object,
        timeout: float | None = None,
    ) -> T:
        admitted_at = time.monotonic()
        try:
            await asyncio.wait_for(
                self._capacity.acquire(), timeout=self.budget.queue_wait_seconds
            )
        except TimeoutError as error:
            raise WorkerOperationError(
                "worker_queue_full", "Bounded worker queue is full"
            ) from error

        self.last_queue_wait_seconds = max(0.0, time.monotonic() - admitted_at)
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        future = loop.run_in_executor(self._executor, operation, *args)
        future.add_done_callback(lambda _future: self._capacity.release())
        try:
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout=timeout or self.budget.provider_timeout_seconds
            )
            self.last_wall_time_seconds = max(0.0, time.monotonic() - started)
            return result
        except TimeoutError as error:
            raise WorkerOperationError(
                "worker_timeout", "Bounded worker operation exceeded its deadline"
            ) from error
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise WorkerOperationError(
                "worker_crashed", str(error)[:200], cause=error
            ) from error

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
