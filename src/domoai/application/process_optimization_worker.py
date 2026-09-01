"""Process-pool-backed CP-SAT worker boundary (spec 150).

`OptimizationWorker` (optimization_worker.py) runs blocking work in a
`ThreadPoolExecutor`. For the CP-SAT solve path specifically, that means a
timed-out solve's thread keeps running to completion in the background --
Python cannot forcibly stop a thread, and `asyncio.wait_for` merely stops
*awaiting* it. `ProcessOptimizationWorker` replaces the thread with a
`pebble.ProcessPool`: `pool.schedule(fn, timeout=N)` genuinely terminates
the OS process running a task that exceeds its deadline, then transparently
respawns a replacement worker for subsequent calls. See
`specs/150-cp-sat-process-isolation/research.md` for the investigation that
led here (why the registry doesn't need to cross the process boundary, why
a fresh-process-per-call design was rejected, why pebble specifically).

Scoped to the CP-SAT solve path only. The energy-context provider worker is
I/O-bound and stays thread-backed (`OptimizationWorker`) -- see spec 150 FR-006.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from collections.abc import Callable

import pebble

from domoai.application.optimization_worker import WorkerBudget, WorkerOperationError
from domoai.domain.models import ErrorDetail
from domoai.optimizer.cp_sat import solve_validated_scenario
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import OptimizationScenario, validate_scenario
from domoai.runtime.registry import DeviceRegistry


def _warm_import_ortools() -> None:
    """pebble worker initializer: pay the ~3.3s ortools cold-import cost
    (measured in research.md Finding 2) once per worker process at
    startup/respawn, not once per solve call."""
    import ortools.sat.python.cp_model  # noqa: F401


class ProcessOptimizationWorker:
    """Runs CP-SAT solves in a persistent, warm pool of OS processes.

    Same public shape as `OptimizationWorker` (`optimize`, `close`,
    `last_queue_wait_seconds`, `last_wall_time_seconds`) so callers that
    only need the CP-SAT boundary don't need to know which backend they
    hold.
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        budget: WorkerBudget | None = None,
        *,
        max_tasks_per_worker: int = 0,
        max_horizon_slots: int = 10080,
        solve_fn: Callable[[OptimizationScenario], OptimizationResult] = solve_validated_scenario,
    ) -> None:
        # `solve_fn` is a test seam (spec 150 SC-001): the function actually
        # scheduled into the pool must be a real, importable module-level
        # callable (spawn re-imports it in the child by qualified name, it
        # is never pickled by value), so tests that need to prove a solve
        # was genuinely killed inject a deliberately slow module-level
        # function from their own test module instead of monkeypatching
        # solve_validated_scenario in-process (which the spawned child,
        # having its own fresh interpreter, would never see).
        self.registry = registry
        self.budget = budget or WorkerBudget()
        self._solve_fn = solve_fn
        self.max_horizon_slots = max_horizon_slots
        self._pool = pebble.ProcessPool(
            max_workers=self.budget.max_concurrency,
            max_tasks=max_tasks_per_worker,
            initializer=_warm_import_ortools,
            context=multiprocessing.get_context("spawn"),
        )
        self._capacity = asyncio.Semaphore(
            self.budget.max_concurrency + self.budget.queue_capacity
        )
        self.last_queue_wait_seconds: float | None = None
        self.last_wall_time_seconds: float | None = None

    async def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        # Validation stays in this (parent) process: it needs the live
        # registry, and it's cheap enough that a process round trip for a
        # reject would be pure waste (spec 150 FR-002).
        diagnostics = validate_scenario(
            scenario, self.registry, max_horizon_slots=self.max_horizon_slots
        )
        if diagnostics:
            return OptimizationResult(
                scenario_id=scenario.id,
                status=OptimizationStatus.INVALID,
                solver="cp-sat",
                diagnostics=diagnostics,
            )

        requested = scenario.solver_time_limit_seconds
        effective = min(requested, self.budget.max_solver_time_seconds)
        capped = scenario.model_copy(update={"solver_time_limit_seconds": effective})
        try:
            result = await self._run_blocking(capped, timeout=max(effective, 0.05))
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

    async def _run_blocking(
        self, scenario: OptimizationScenario, *, timeout: float
    ) -> OptimizationResult:
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
        started = time.monotonic()
        # `timeout=` here is pebble's own, server-side enforced deadline:
        # exceeding it terminates the OS process actually running the task
        # (not merely abandons a client-side await, as the thread-backed
        # OptimizationWorker's asyncio.wait_for does). This is the whole
        # point of this class -- see spec 150 FR-001/FR-004.
        process_future = self._pool.schedule(self._solve_fn, args=[scenario], timeout=timeout)
        future = asyncio.wrap_future(process_future)
        future.add_done_callback(lambda _future: self._capacity.release())
        try:
            result: OptimizationResult = await future
            self.last_wall_time_seconds = max(0.0, time.monotonic() - started)
            return result
        except TimeoutError as error:
            # Python 3.11+ unifies asyncio.TimeoutError and
            # concurrent.futures.TimeoutError as the builtin TimeoutError,
            # so this catches pebble's server-side deadline the same way
            # the capacity-acquire timeout above is caught.
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
        self._pool.stop()  # type: ignore[no-untyped-call]  # pebble's BasePool.stop lacks a return annotation upstream
        self._pool.join(timeout=5)
