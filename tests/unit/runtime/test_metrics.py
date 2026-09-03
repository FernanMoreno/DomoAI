from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.event_consumer import RuntimeEventConsumer
from domoai.application.executor import PlanExecutor
from domoai.application.metrics import RuntimeMetricsCollector
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.scheduler import Scheduler
from domoai.domain.models import (
    Command,
    Plan,
    PlanStatus,
    StateSnapshot,
    StateStatus,
)
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus, SolverEvidence
from domoai.persistence.repositories import (
    PlanRepository,
    RecurringScheduleRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


class _FixedOptimizer:
    def __init__(self, result: OptimizationResult) -> None:
        self._result = result

    def optimize(self, scenario: object) -> OptimizationResult:
        del scenario
        return self._result


async def _build_collector(
    tmp_path: Path,
) -> tuple[RuntimeMetricsCollector, DeviceRegistry, StateStore, PlanRepository, SQLiteDatabase]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await adapter.connect()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    scheduled_plan_repository = ScheduledPlanRepository(database)
    recurring_repository = RecurringScheduleRepository(database)
    scheduler = Scheduler(
        executor, scheduled_plan_repository, audit, recurring_repository=recurring_repository
    )
    event_consumer = RuntimeEventConsumer(
        adapter, DiscoveryService(adapter, registry, state_store, audit), state_store, audit
    )
    collector = RuntimeMetricsCollector(
        adapter=adapter,
        event_consumer=event_consumer,
        scheduler=scheduler,
        state_store=state_store,
        plan_repository=plan_repository,
        database=database,
    )
    return collector, registry, state_store, plan_repository, database


@pytest.mark.asyncio
async def test_snapshot_has_every_key_with_defaults_on_a_fresh_runtime(tmp_path: Path) -> None:
    collector, *_ = await _build_collector(tmp_path)

    snapshot = await collector.snapshot()

    assert snapshot["schema_version"] == "v1"
    assert "generated_at" in snapshot
    assert snapshot["adapter_health"]["connected"] is True
    assert snapshot["event_consumer_alive"] is False
    assert snapshot["scheduler_alive"] is False
    assert snapshot["event_queue_depth"] == {"bulk": 0, "priority": 0}
    assert snapshot["dropped_events_total"] == 0
    assert snapshot["dropped_events_by_adapter"] == {}
    assert snapshot["dropped_events_by_kind"] == {}
    assert snapshot["coalesced_events_total"] == 0
    assert snapshot["stale_state_count"] == 0
    assert snapshot["plans_by_status"] == {"pending": 0, "executing": 0, "unknown": 0}
    assert snapshot["optimizer_last_wall_time_seconds"] is None
    assert snapshot["db_operation_count"] >= 0
    assert snapshot["db_busy_count"] == 0


@pytest.mark.asyncio
async def test_snapshot_uses_injected_clock_for_generated_at(tmp_path: Path) -> None:
    collector, *_ = await _build_collector(tmp_path)
    collector.clock = FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC))

    snapshot = await collector.snapshot()

    assert snapshot["generated_at"] == "2026-08-23T12:00:00+00:00"


@pytest.mark.asyncio
async def test_event_queue_depth_reports_zero_for_a_non_composite_adapter(tmp_path: Path) -> None:
    collector, *_ = await _build_collector(tmp_path)

    snapshot = await collector.snapshot()

    assert snapshot["event_queue_depth"] == {"bulk": 0, "priority": 0}


@pytest.mark.asyncio
async def test_stale_state_count_reflects_marked_stale_snapshots(tmp_path: Path) -> None:
    collector, registry, state_store, _plan_repository, _database = await _build_collector(tmp_path)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    existing = state_store.peek(device_id, "power")
    assert existing is not None
    await state_store.save(
        StateSnapshot(
            device_id=device_id,
            capability="power",
            value=True,
            observed_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            status=StateStatus.STALE,
            source_ref=existing.source_ref,
        )
    )

    snapshot = await collector.snapshot()

    assert snapshot["stale_state_count"] >= 1


@pytest.mark.asyncio
async def test_plans_by_status_counts_matching_plans(tmp_path: Path) -> None:
    collector, registry, _state_store, plan_repository, _database = await _build_collector(tmp_path)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")

    ready_plan = Plan(
        id="metrics-plan-ready",
        status=PlanStatus.READY,
        commands=[
            Command(
                id="metrics-cmd-ready",
                device_id=device_id,
                command="turn_on",
                idempotency_key="metrics-intent-ready",
            )
        ],
    )
    executing_plan = ready_plan.model_copy(
        update={"id": "metrics-plan-executing", "status": PlanStatus.EXECUTING}
    )
    unknown_plan = ready_plan.model_copy(
        update={"id": "metrics-plan-unknown", "status": PlanStatus.UNKNOWN}
    )
    await plan_repository.save(ready_plan)
    await plan_repository.save(executing_plan)
    await plan_repository.save(unknown_plan)

    snapshot = await collector.snapshot()

    assert snapshot["plans_by_status"] == {"pending": 1, "executing": 1, "unknown": 1}


@pytest.mark.asyncio
async def test_optimizer_last_wall_time_seconds_reflected_once_optimization_service_wired(
    tmp_path: Path,
) -> None:
    collector, registry, state_store, plan_repository, database = await _build_collector(tmp_path)
    plan_service = PlanService(registry, state_store, PolicyEngine([]), AuditLog())
    result = OptimizationResult(
        scenario_id="fixture-scenario",
        status=OptimizationStatus.OPTIMAL,
        solver="fake",
        solver_evidence=SolverEvidence(
            solver_name="cp-sat",
            solver_version="test",
            num_search_workers=1,
            random_seed=0,
            wall_time_seconds=0.42,
            tiers=[],
            scenario_fingerprint="fp",
        ),
    )
    optimization_service = OptimizationService(registry, plan_service, _FixedOptimizer(result))
    optimization_service.optimize(object())  # type: ignore[arg-type]
    collector.optimization_service = optimization_service

    snapshot = await collector.snapshot()

    assert snapshot["optimizer_last_wall_time_seconds"] == 0.42


@pytest.mark.asyncio
async def test_composite_adapter_reports_queue_depth_and_dropped_events(tmp_path: Path) -> None:
    home_assistant = RecordingAdapter(
        "home_assistant", source_snapshot(adapter_id="home_assistant")
    )
    registry = DeviceRegistry()
    composite = CompositeAdapter([home_assistant], registry=registry, event_queue_max_size=1000)
    state_store = StateStore()
    audit = AuditLog()
    await composite.connect()
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    discovery = DiscoveryService(composite, registry, state_store, audit)
    event_consumer = RuntimeEventConsumer(composite, discovery, state_store, audit)
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(composite, plan_service, audit, plan_repository=plan_repository)
    scheduler = Scheduler(
        executor,
        ScheduledPlanRepository(database),
        audit,
        recurring_repository=RecurringScheduleRepository(database),
    )
    collector = RuntimeMetricsCollector(
        adapter=composite,
        event_consumer=event_consumer,
        scheduler=scheduler,
        state_store=state_store,
        plan_repository=plan_repository,
        database=database,
    )

    snapshot = await collector.snapshot()

    assert snapshot["event_queue_depth"] == {"bulk": 0, "priority": 0}
    assert snapshot["dropped_events_total"] == 0
    assert snapshot["dropped_events_by_adapter"] == {}
    assert snapshot["dropped_events_by_kind"] == {}
    assert snapshot["coalesced_events_total"] == 0
