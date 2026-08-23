from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.optimization_worker import OptimizationWorker, WorkerBudget
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import Horizon, OptimizationScenario


def _scenario(*, requested_seconds: float = 60.0) -> OptimizationScenario:
    start = datetime(2026, 8, 23, 12, tzinfo=UTC)
    return OptimizationScenario(
        id="worker-test-scenario",
        horizon=Horizon(
            start=start,
            end=start + timedelta(minutes=15),
            resolution_minutes=15,
            timezone="Europe/Madrid",
        ),
        solver_time_limit_seconds=requested_seconds,
    )


class _RecordingService:
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.calls: list[OptimizationScenario] = []

    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        self.calls.append(scenario)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return OptimizationResult(
            scenario_id=scenario.id,
            status=OptimizationStatus.FEASIBLE,
            solver="fixture",
        )


@pytest.mark.asyncio
async def test_worker_caps_caller_budget_at_deployment_limit() -> None:
    service = _RecordingService()
    worker = OptimizationWorker(
        service,
        WorkerBudget(
            max_solver_time_seconds=0.25,
            queue_capacity=1,
            max_concurrency=1,
            queue_wait_seconds=0.1,
            provider_timeout_seconds=0.25,
        ),
    )

    result = await worker.optimize(_scenario(requested_seconds=600.0))

    assert result.status is OptimizationStatus.FEASIBLE
    assert service.calls[0].solver_time_limit_seconds == 0.25


@pytest.mark.asyncio
async def test_worker_timeout_returns_non_executable_typed_result() -> None:
    worker = OptimizationWorker(
        _RecordingService(delay_seconds=0.2),
        WorkerBudget(
            max_solver_time_seconds=0.01,
            queue_capacity=0,
            max_concurrency=1,
            queue_wait_seconds=0.01,
            provider_timeout_seconds=0.01,
        ),
    )

    result = await worker.optimize(_scenario(requested_seconds=60.0))

    assert result.status is OptimizationStatus.TIMEOUT
    assert result.plan is None
    assert any(item.code == "worker_timeout" for item in result.diagnostics)


@pytest.mark.asyncio
async def test_worker_queue_is_bounded_and_reports_rejection() -> None:
    service = _RecordingService(delay_seconds=0.2)
    worker = OptimizationWorker(
        service,
        WorkerBudget(
            max_solver_time_seconds=1.0,
            queue_capacity=0,
            max_concurrency=1,
            queue_wait_seconds=0.01,
            provider_timeout_seconds=1.0,
        ),
    )

    first = pytest.importorskip("asyncio").create_task(worker.optimize(_scenario()))
    await pytest.importorskip("asyncio").sleep(0.02)
    second = await worker.optimize(_scenario(requested_seconds=2.0))
    await first

    assert second.status is OptimizationStatus.UNKNOWN
    assert any(item.code == "worker_queue_full" for item in second.diagnostics)
