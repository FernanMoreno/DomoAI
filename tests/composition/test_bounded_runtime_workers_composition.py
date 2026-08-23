from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_worker import OptimizationWorker, WorkerBudget
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import Horizon, OptimizationScenario
from domoai.persistence.repositories import PlanRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore


class _BlockingOptimizer:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.started = threading.Event()
        self.calls: list[OptimizationScenario] = []

    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        self.calls.append(scenario)
        self.started.set()
        time.sleep(self.delay_seconds)
        return OptimizationResult(
            scenario_id=scenario.id,
            status=OptimizationStatus.FEASIBLE,
            solver="fixture",
        )


def _scenario() -> OptimizationScenario:
    start = datetime(2026, 8, 23, 12, tzinfo=UTC)
    return OptimizationScenario(
        id="composition-worker-scenario",
        horizon=Horizon(
            start=start,
            end=start + timedelta(minutes=15),
            resolution_minutes=15,
            timezone="Europe/Madrid",
        ),
        solver_time_limit_seconds=600,
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_blocking_optimizer_does_not_starve_due_scheduler(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = SimulatedHomeAdapter(clock=clock)
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")

    database = SQLiteDatabase(tmp_path / "bounded-worker.sqlite3", clock=clock)
    await database.initialize()
    plan_repository = PlanRepository(database, clock=clock)
    scheduled_repository = ScheduledPlanRepository(database, clock=clock)
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    scheduler = Scheduler(executor, scheduled_repository, audit, clock=clock)
    plan = plan_service.validate(
        Plan(
            id="composition-worker-plan",
            execute_at=now - timedelta(seconds=1),
            commands=[
                Command(
                    id="composition-worker-command",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="composition-worker-intent",
                )
            ],
        )
    )
    await plan_repository.save_validation(plan)
    await scheduled_repository.schedule(plan)

    optimizer = _BlockingOptimizer(delay_seconds=0.2)
    worker = OptimizationWorker(
        optimizer,
        WorkerBudget(
            max_solver_time_seconds=0.05,
            queue_capacity=1,
            max_concurrency=1,
            queue_wait_seconds=0.1,
            provider_timeout_seconds=0.2,
        ),
    )
    worker_task = asyncio.create_task(worker.optimize(_scenario()))
    assert await asyncio.to_thread(optimizer.started.wait, 1.0)
    started = time.monotonic()
    scheduler_result = await scheduler.run_due()
    scheduler_elapsed = time.monotonic() - started
    worker_result = await worker_task

    assert scheduler_result == [{"plan_id": plan.id, "outcome": "executed"}]
    assert scheduler_elapsed < 0.15
    assert worker_result.status is OptimizationStatus.TIMEOUT
    assert optimizer.calls[0].solver_time_limit_seconds == 0.05
    assert len(adapter.calls) == 1
