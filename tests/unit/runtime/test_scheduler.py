from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan, RecurrenceRule, RiskClass
from domoai.persistence.repositories import RecurringScheduleRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore


async def _build_scheduler(
    tmp_path, *, grace_window: timedelta = timedelta(minutes=15)
) -> tuple[SimulatedHomeAdapter, PlanService, Scheduler, ScheduledPlanRepository, AuditLog]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ScheduledPlanRepository(database)
    recurring_repository = RecurringScheduleRepository(database)
    scheduler = Scheduler(
        executor,
        repository,
        audit,
        grace_window=grace_window,
        recurring_repository=recurring_repository,
    )
    return adapter, plan_service, scheduler, repository, audit


def _command(device_id: str, *, plan_id: str, risk_class: RiskClass = RiskClass.SAFE) -> Command:
    return Command(
        id=f"{plan_id}:command",
        device_id=device_id,
        command="set_brightness",
        value=60,
        unit="%",
        idempotency_key=f"{plan_id}:intent",
        risk_class=risk_class,
    )


def _plan(device_id: str, *, plan_id: str, execute_at: datetime) -> Plan:
    return Plan(
        id=plan_id,
        execute_at=execute_at,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )


@pytest.mark.asyncio
async def test_due_plan_within_grace_window_executes(tmp_path) -> None:
    adapter, plan_service, scheduler, repository, _ = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan = _plan(
        device_id, plan_id="plan-due-1", execute_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    validated = plan_service.validate(plan)
    await scheduler.schedule(validated)

    results = await scheduler.run_due()

    assert results == [{"plan_id": "plan-due-1", "outcome": "executed"}]
    assert len(adapter.calls) == 1
    _, status = await repository.get("plan-due-1")
    assert status == "executed"


@pytest.mark.asyncio
async def test_overdue_beyond_grace_window_is_marked_missed(tmp_path) -> None:
    adapter, plan_service, scheduler, repository, audit = await _build_scheduler(
        tmp_path, grace_window=timedelta(minutes=5)
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan = _plan(
        device_id, plan_id="plan-missed-1", execute_at=datetime.now(UTC) - timedelta(minutes=30)
    )
    validated = plan_service.validate(plan)
    await scheduler.schedule(validated)

    results = await scheduler.run_due()

    assert results == [{"plan_id": "plan-missed-1", "outcome": "missed"}]
    assert adapter.calls == []
    _, status = await repository.get("plan-missed-1")
    assert status == "missed"
    assert any(event.event_type == "schedule_missed" for event in audit.events)


@pytest.mark.asyncio
async def test_sweep_uses_one_consistent_now_for_every_row(tmp_path) -> None:
    adapter, plan_service, scheduler, repository, _ = await _build_scheduler(
        tmp_path, grace_window=timedelta(minutes=5)
    )
    light_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    switch_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "switch"
    )
    now = datetime.now(UTC)
    plan_a = plan_service.validate(
        _plan(light_id, plan_id="plan-sweep-a", execute_at=now - timedelta(minutes=1))
    )
    plan_b = plan_service.validate(
        Plan(
            id="plan-sweep-b",
            execute_at=now - timedelta(minutes=2),
            commands=[
                Command(
                    id="plan-sweep-b:command",
                    device_id=switch_id,
                    command="turn_on",
                    idempotency_key="plan-sweep-b:intent",
                )
            ],
        )
    )
    await scheduler.schedule(plan_a)
    await scheduler.schedule(plan_b)

    results = await scheduler.run_due(now=now)

    outcomes = {item["plan_id"]: item["outcome"] for item in results}
    assert outcomes == {"plan-sweep-a": "executed", "plan-sweep-b": "executed"}
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_pending_schedule_survives_a_fresh_scheduler_instance(tmp_path) -> None:
    adapter, plan_service, scheduler, _repository, _ = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan = plan_service.validate(
        _plan(
            device_id,
            plan_id="plan-restart-1",
            execute_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await scheduler.schedule(plan)

    restarted_database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await restarted_database.initialize()
    restarted_repository = ScheduledPlanRepository(restarted_database)
    restarted_scheduler = Scheduler(scheduler.executor, restarted_repository, scheduler.audit)

    pending = await restarted_scheduler.list_pending()
    assert [item.id for item in pending] == ["plan-restart-1"]

    results = await restarted_scheduler.run_due()
    assert results == [{"plan_id": "plan-restart-1", "outcome": "executed"}]
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_cancel_prevents_execution(tmp_path) -> None:
    adapter, plan_service, scheduler, _repository, _ = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan = plan_service.validate(
        _plan(
            device_id,
            plan_id="plan-cancel-1",
            execute_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await scheduler.schedule(plan)

    assert await scheduler.cancel("plan-cancel-1") is True
    assert [item.id for item in await scheduler.list_pending()] == []

    results = await scheduler.run_due()
    assert results == []
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_reschedule_moves_execution_time(tmp_path) -> None:
    _, plan_service, scheduler, _repository, _ = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan = plan_service.validate(
        _plan(
            device_id,
            plan_id="plan-reschedule-1",
            execute_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await scheduler.schedule(plan)

    new_time = datetime.now(UTC) + timedelta(hours=2)
    assert await scheduler.reschedule("plan-reschedule-1", new_time) is True
    pending = await scheduler.list_pending()
    assert pending[0].execute_at == new_time


@pytest.mark.asyncio
async def test_due_recurring_schedule_executes_and_advances(tmp_path) -> None:
    adapter, plan_service, scheduler, _repository, _ = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    schedule_id = "recurring-1"
    await scheduler.recurring_repository.create(
        schedule_id,
        [_command(device_id, plan_id=schedule_id)],
        rule,
        datetime.now(UTC) - timedelta(minutes=1),
    )

    results = await scheduler.run_due_recurring()

    assert results == [{"schedule_id": schedule_id, "outcome": "executed"}]
    assert len(adapter.calls) == 1
    active = await scheduler.list_recurring()
    assert active[0][0] == schedule_id
    assert active[0][3] > datetime.now(UTC)


@pytest.mark.asyncio
async def test_run_due_recurring_does_not_double_execute_before_next_occurrence(
    tmp_path,
) -> None:
    adapter, plan_service, scheduler, _repository, _ = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    schedule_id = "recurring-2"
    await scheduler.recurring_repository.create(
        schedule_id,
        [_command(device_id, plan_id=schedule_id)],
        rule,
        datetime.now(UTC) - timedelta(minutes=1),
    )

    await scheduler.run_due_recurring()
    second_sweep = await scheduler.run_due_recurring()

    assert second_sweep == []
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_recurring_occurrence_is_skipped_when_device_no_longer_exists(tmp_path) -> None:
    adapter, plan_service, scheduler, _repository, _ = await _build_scheduler(tmp_path)
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    schedule_id = "recurring-invalid"
    await scheduler.recurring_repository.create(
        schedule_id,
        [_command("no-such-device", plan_id=schedule_id)],
        rule,
        datetime.now(UTC) - timedelta(minutes=1),
    )

    results = await scheduler.run_due_recurring()

    assert results == [{"schedule_id": schedule_id, "outcome": "skipped"}]
    assert adapter.calls == []
    active = await scheduler.list_recurring()
    assert active[0][3] > datetime.now(UTC)


@pytest.mark.asyncio
async def test_recurring_occurrence_requiring_confirmation_is_skipped_never_auto_approved(
    tmp_path,
) -> None:
    adapter, plan_service, scheduler, _repository, audit = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    schedule_id = "recurring-confirm"
    await scheduler.recurring_repository.create(
        schedule_id,
        [_command(device_id, plan_id=schedule_id, risk_class=RiskClass.CONFIRM)],
        rule,
        datetime.now(UTC) - timedelta(minutes=1),
    )

    results = await scheduler.run_due_recurring()

    assert results == [{"schedule_id": schedule_id, "outcome": "skipped"}]
    assert adapter.calls == []
    skipped_events = [
        event for event in audit.events if event.event_type == "recurring_occurrence_skipped"
    ]
    assert len(skipped_events) == 1
    assert skipped_events[0].payload["reason"] == "requires_confirmation"
    assert skipped_events[0].payload["schedule_id"] == schedule_id


@pytest.mark.asyncio
async def test_recurring_schedule_continues_after_a_skipped_occurrence(tmp_path) -> None:
    adapter, plan_service, scheduler, _repository, _ = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    schedule_id = "recurring-recover"
    await scheduler.recurring_repository.create(
        schedule_id,
        [_command(device_id, plan_id=schedule_id, risk_class=RiskClass.CONFIRM)],
        rule,
        datetime.now(UTC) - timedelta(minutes=1),
    )

    await scheduler.run_due_recurring()
    active = await scheduler.list_recurring()
    next_time = active[0][3]

    second_results = await scheduler.run_due_recurring(now=next_time)

    assert second_results == [{"schedule_id": schedule_id, "outcome": "skipped"}]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_cancel_recurring_schedule_stops_future_occurrences(tmp_path) -> None:
    adapter, plan_service, scheduler, _repository, _ = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    schedule_id = "recurring-cancel"
    await scheduler.schedule_recurring(
        schedule_id, [_command(device_id, plan_id=schedule_id)], rule
    )

    assert await scheduler.cancel_recurring(schedule_id) is True
    assert await scheduler.list_recurring() == []

    results = await scheduler.run_due_recurring(now=datetime.now(UTC) + timedelta(days=2))
    assert results == []
    assert adapter.calls == []
