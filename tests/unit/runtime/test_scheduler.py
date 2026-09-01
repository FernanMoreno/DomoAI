from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.execution_admission import ExecutionAdmission
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.scheduler import Scheduler
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    BundleMemberCommitStatus,
    Command,
    CommandPostcondition,
    Plan,
    PlanStatus,
    Precondition,
    RecurrenceRule,
    RiskClass,
    StateStatus,
)
from domoai.persistence.repositories import (
    BundleCommitRepository,
    PlanRepository,
    RecurringScheduleRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import Clock, FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def _build_scheduler(
    tmp_path,
    *,
    grace_window: timedelta = timedelta(minutes=15),
    clock: Clock | None = None,
    durable: bool = False,
    bundle_aware: bool = False,
) -> tuple[SimulatedHomeAdapter, PlanService, Scheduler, ScheduledPlanRepository, AuditLog]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database) if durable else None
    repository = ScheduledPlanRepository(database)
    recurring_repository = RecurringScheduleRepository(database)
    bundle_repository = BundleCommitRepository(database) if bundle_aware else None
    execution_admission = (
        ExecutionAdmission(bundle_repository=bundle_repository) if bundle_aware else None
    )
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        clock=clock,
        plan_repository=plan_repository,
        execution_admission=execution_admission,
    )
    scheduler = Scheduler(
        executor,
        repository,
        audit,
        grace_window=grace_window,
        recurring_repository=recurring_repository,
        bundle_repository=bundle_repository,
        execution_admission=execution_admission,
        clock=clock,
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


def _bundle_repository(scheduler: Scheduler) -> BundleCommitRepository:
    repository = scheduler.bundle_repository
    assert repository is not None
    return repository


async def _build_bundle_member(
    plan_service: PlanService,
    scheduler: Scheduler,
    repository: ScheduledPlanRepository,
    *,
    plan_id: str,
    execute_at: datetime,
    scheduled: bool,
) -> Plan:
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan = plan_service.validate(_plan(device_id, plan_id=plan_id, execute_at=execute_at))
    bundle = BundleCommit(
        id=f"bundle-{plan_id}",
        bundle_digest=f"sha256:{plan_id}-bundle",
        scenario_id=f"scenario-{plan_id}",
        status=BundleCommitStatus.SCHEDULED if scheduled else BundleCommitStatus.COMMITTING,
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest=plan.validation.digest if plan.validation else "missing",
                execute_at=plan.execute_at,
                status=(
                    BundleMemberCommitStatus.SCHEDULED
                    if scheduled
                    else BundleMemberCommitStatus.PENDING
                ),
                scheduled=scheduled,
            )
        ],
    )
    await _bundle_repository(scheduler).save(bundle)
    if scheduled:
        await repository.schedule(plan)
    return plan


class _StopTest(BaseException):
    """Sentinel used to deterministically end a `run()` loop under test.

    Must be a `BaseException`, not `Exception` — `Scheduler.run()`'s own
    `except Exception` isolation (this feature) would otherwise swallow it
    like any other unexpected failure, hanging the test forever.
    """


class _FailingExecutorWrapper:
    """Delegates to a real PlanExecutor, except raising for one matching plan id."""

    def __init__(self, real_executor: PlanExecutor, failing_plan_prefix: str) -> None:
        self._real = real_executor
        self._failing_plan_prefix = failing_plan_prefix
        self.plan_service = real_executor.plan_service

    async def execute(self, plan: Plan, *, aggregate_owner: bool = False):
        assert aggregate_owner is True
        if plan.id.startswith(self._failing_plan_prefix):
            raise RuntimeError("simulated unexpected execution failure")
        return await self._real.execute(plan)


@pytest.mark.asyncio
async def test_one_plan_failure_does_not_abandon_other_due_plans_in_sweep(tmp_path) -> None:
    adapter, plan_service, scheduler, repository, audit = await _build_scheduler(tmp_path)
    # Distinct devices per plan — sharing one device would make a later
    # plan's execution legitimately raise DomainError(STALE_PLAN) once an
    # earlier plan in the sweep mutates that device's state, which would
    # confound this test's isolation assertion with an unrelated, already
    # correct safety mechanism.
    light_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    switch_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "switch"
    )
    climate_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "climate"
    )
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    plans = {
        "plan-a": Plan(
            id="plan-a",
            execute_at=due_at,
            commands=[
                Command(
                    id="plan-a:command",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="plan-a:intent",
                )
            ],
        ),
        "plan-b": Plan(
            id="plan-b",
            execute_at=due_at,
            commands=[
                Command(
                    id="plan-b:command",
                    device_id=switch_id,
                    command="turn_on",
                    idempotency_key="plan-b:intent",
                )
            ],
        ),
        "plan-c": Plan(
            id="plan-c",
            execute_at=due_at,
            commands=[
                Command(
                    id="plan-c:command",
                    device_id=climate_id,
                    command="set_temperature",
                    value=22,
                    unit="°C",
                    idempotency_key="plan-c:intent",
                    # Spec 165 made climate.bedroom's set_temperature a real,
                    # confirmable command (previously dead code that always
                    # left target_temperature unchanged, which is what
                    # incidentally produced "unknown" here before -- an
                    # accidental coupling to a bug, not a deliberate test
                    # design). An explicit postcondition with a value the
                    # fixture will never report keeps this test's original
                    # intent (prove "unknown" outcomes survive sweep
                    # continuation) genuinely independent of the fixture's
                    # real behavior.
                    postconditions=[
                        CommandPostcondition(capability="target_temperature", expected=999.0)
                    ],
                )
            ],
        ),
    }
    for plan in plans.values():
        validated = plan_service.validate(plan)
        await scheduler.schedule(validated)
    scheduler.executor = _FailingExecutorWrapper(scheduler.executor, "plan-b")

    results = await scheduler.run_due()

    outcomes = {entry["plan_id"]: entry["outcome"] for entry in results}
    assert outcomes["plan-a"] == "executed"
    assert outcomes["plan-b"] == "error"
    assert outcomes["plan-c"] == "unknown"
    assert len(adapter.calls) == 2
    assert any(event.event_type == "schedule_execution_error" for event in audit.events)


@pytest.mark.asyncio
async def test_one_recurring_occurrence_failure_does_not_abandon_other_due_schedules(
    tmp_path,
) -> None:
    adapter, plan_service, scheduler, _repository, audit = await _build_scheduler(tmp_path)
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    for schedule_id in ("recurring-a", "recurring-b", "recurring-c"):
        await scheduler.recurring_repository.create(
            schedule_id, [_command(device_id, plan_id=schedule_id)], rule, due_at
        )
    scheduler.executor = _FailingExecutorWrapper(scheduler.executor, "recurring-b@")

    results = await scheduler.run_due_recurring()

    outcomes = {entry["schedule_id"]: entry["outcome"] for entry in results}
    assert outcomes["recurring-a"] == "executed"
    assert outcomes["recurring-b"] == "error"
    assert outcomes["recurring-c"] == "executed"
    assert len(adapter.calls) == 2
    assert any(event.event_type == "recurring_occurrence_error" for event in audit.events)


@pytest.mark.asyncio
async def test_run_loop_survives_a_sweep_failure_and_reaches_next_poll_cycle(tmp_path) -> None:
    _adapter, _plan_service, scheduler, _repository, audit = await _build_scheduler(
        tmp_path, clock=FixedClock(datetime.now(UTC))
    )
    scheduler.poll_interval = timedelta(seconds=0)

    call_count = 0
    original_run_due = scheduler.run_due

    async def flaky_run_due(*args: object, **kwargs: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated unexpected sweep failure")
        if call_count == 2:
            raise _StopTest
        return await original_run_due(*args, **kwargs)

    scheduler.run_due = flaky_run_due  # type: ignore[method-assign]

    with pytest.raises(_StopTest):
        await scheduler.run()

    assert call_count == 2
    assert any(event.event_type == "schedule_sweep_error" for event in audit.events)


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
async def test_due_plan_with_stale_precondition_is_not_marked_executed(tmp_path) -> None:
    adapter, plan_service, scheduler, repository, _ = await _build_scheduler(tmp_path)
    light_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    switch_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "switch"
    )
    source = await plan_service.state_store.get(switch_id, "power")
    assert source is not None
    await plan_service.state_store.save(source.model_copy(update={"status": StateStatus.STALE}))
    plan = plan_service.validate(
        Plan(
            id="plan-scheduled-stale-precondition",
            execute_at=datetime.now(UTC) - timedelta(minutes=1),
            commands=[
                Command(
                    id="plan-scheduled-stale-precondition:command",
                    device_id=light_id,
                    command="set_brightness",
                    value=60,
                    unit="%",
                    idempotency_key="plan-scheduled-stale-precondition:intent",
                    preconditions=[
                        Precondition(
                            device_id=switch_id,
                            capability="power",
                            expected=source.value,
                        )
                    ],
                )
            ],
        )
    )
    await scheduler.schedule(plan)

    results = await scheduler.run_due()

    assert results == [{"plan_id": plan.id, "outcome": "failed"}]
    assert adapter.calls == []
    _, status = await repository.get(plan.id)
    assert status == "failed"


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
async def test_pending_schedule_with_executing_evidence_becomes_unknown_without_replay(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    adapter, plan_service, scheduler, repository, audit = await _build_scheduler(
        tmp_path, clock=FixedClock(now), durable=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan = plan_service.validate(
        _plan(device_id, plan_id="plan-orphaned-executing", execute_at=now - timedelta(minutes=1))
    )
    await scheduler.schedule(plan)
    assert scheduler.executor.plan_repository is not None
    await scheduler.executor.plan_repository.save(
        plan.model_copy(update={"status": PlanStatus.EXECUTING})
    )

    results = await scheduler.run_due(now=now)

    assert results == [{"plan_id": "plan-orphaned-executing", "outcome": "reconciled"}]
    assert adapter.calls == []
    _scheduled_plan, status = await repository.get("plan-orphaned-executing")
    assert status == "unknown"
    assert any(event.event_type == "schedule_execution_reconciled" for event in audit.events)


@pytest.mark.asyncio
async def test_recurring_executing_evidence_advances_without_replay(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    adapter, plan_service, scheduler, _repository, audit = await _build_scheduler(
        tmp_path, clock=FixedClock(now), durable=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(12, 0), timezone="UTC")
    due_at = now - timedelta(days=1)
    schedule_id = "recurring-orphaned-executing"
    command = _command(device_id, plan_id=schedule_id)
    await scheduler.recurring_repository.create(schedule_id, [command], rule, due_at)
    assert scheduler.executor.plan_repository is not None
    occurrence_id = f"{schedule_id}@{due_at.isoformat()}"
    await scheduler.executor.plan_repository.save(
        Plan(id=occurrence_id, commands=[command], status=PlanStatus.EXECUTING)
    )

    results = await scheduler.run_due_recurring(now=now)

    assert results == [{"schedule_id": schedule_id, "outcome": "reconciled"}]
    assert adapter.calls == []
    active = await scheduler.recurring_repository.list_active()
    assert active[0][3] > due_at
    assert any(event.event_type == "recurring_occurrence_reconciled" for event in audit.events)


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
async def test_reschedule_requires_a_new_temporal_revision(tmp_path) -> None:
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
    assert await scheduler.reschedule("plan-reschedule-1", new_time) is False
    pending = await scheduler.list_pending()
    assert pending[0].execute_at == plan.execute_at


@pytest.mark.asyncio
async def test_cancel_rejects_a_pending_bundle_member_without_settling_schedule(tmp_path) -> None:
    _, plan_service, scheduler, repository, _ = await _build_scheduler(
        tmp_path, bundle_aware=True
    )
    member = await _build_bundle_member(
        plan_service,
        scheduler,
        repository,
        plan_id="plan-scheduler-cancel-member",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        scheduled=True,
    )

    with pytest.raises(DomainError) as excinfo:
        await scheduler.cancel(member.id)

    assert excinfo.value.code is ErrorCode.BUNDLE_MEMBER_CANCEL_FORBIDDEN
    stored = await repository.get(member.id)
    assert stored is not None
    assert stored[1] == "pending"


@pytest.mark.asyncio
async def test_reschedule_rejects_a_pending_bundle_member_without_changing_temporal_evidence(
    tmp_path,
) -> None:
    _, plan_service, scheduler, repository, _ = await _build_scheduler(
        tmp_path, bundle_aware=True
    )
    member = await _build_bundle_member(
        plan_service,
        scheduler,
        repository,
        plan_id="plan-scheduler-reschedule-member",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        scheduled=True,
    )
    stored_before = await repository.get(member.id)
    assert stored_before is not None
    original_plan, original_status = stored_before
    assert original_status == "pending"
    new_execute_at = original_plan.execute_at + timedelta(hours=1)

    with pytest.raises(DomainError) as excinfo:
        await scheduler.reschedule(member.id, new_execute_at)

    assert excinfo.value.code is ErrorCode.BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN
    stored_after = await repository.get(member.id)
    assert stored_after is not None
    assert stored_after[1] == "pending"
    assert stored_after[0].execute_at == original_plan.execute_at
    assert stored_after[0].schedule_revision == original_plan.schedule_revision


@pytest.mark.asyncio
async def test_schedule_rejects_a_bundle_member_for_a_generic_caller(tmp_path) -> None:
    _, plan_service, scheduler, repository, _ = await _build_scheduler(
        tmp_path, bundle_aware=True
    )
    member = await _build_bundle_member(
        plan_service,
        scheduler,
        repository,
        plan_id="plan-scheduler-schedule-member",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        scheduled=False,
    )

    with pytest.raises(DomainError) as excinfo:
        await scheduler.schedule(member)

    assert excinfo.value.code is ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN
    assert await repository.get(member.id) is None


@pytest.mark.asyncio
async def test_scheduler_allows_non_member_mutations_with_bundle_admission_enabled(
    tmp_path,
) -> None:
    _, plan_service, scheduler, repository, _ = await _build_scheduler(
        tmp_path, bundle_aware=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan = plan_service.validate(
        _plan(
            device_id,
            plan_id="plan-scheduler-non-member",
            execute_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )

    await scheduler.schedule(plan)

    assert await scheduler.cancel(plan.id) is True
    stored = await repository.get(plan.id)
    assert stored is not None
    assert stored[1] == "cancelled"


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


@pytest.mark.asyncio
async def test_recurring_schedule_with_expires_at_stops_firing_past_expiry(tmp_path) -> None:
    now = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    clock = FixedClock(now)
    adapter, plan_service, scheduler, _repository, audit = await _build_scheduler(
        tmp_path, clock=clock
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(
        time_of_day=time(0, 0),
        timezone="UTC",
        expires_at=now + timedelta(days=1, minutes=30),
    )
    schedule_id = "recurring-expires"
    await scheduler.schedule_recurring(
        schedule_id, [_command(device_id, plan_id=schedule_id)], rule
    )

    # First occurrence (day 1) is before expires_at: fires normally.
    first_results = await scheduler.run_due_recurring(now=now + timedelta(days=1))
    assert first_results == [{"schedule_id": schedule_id, "outcome": "executed"}]
    assert len(adapter.calls) == 1
    assert await scheduler.list_recurring() != []

    # Second occurrence (day 2) is past expires_at: the schedule expires
    # instead of firing, and the audit trail records why.
    second_results = await scheduler.run_due_recurring(now=now + timedelta(days=2))
    assert second_results == [{"schedule_id": schedule_id, "outcome": "expired"}]
    assert len(adapter.calls) == 1  # unchanged -- no second dispatch
    assert await scheduler.list_recurring() == []
    assert any(event.event_type == "recurring_schedule_expired" for event in audit.events)


@pytest.mark.asyncio
async def test_run_due_uses_injected_clock_when_no_explicit_now_given(tmp_path) -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    adapter, plan_service, scheduler, repository, audit = await _build_scheduler(
        tmp_path, clock=clock
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan_id = "clock-due-1"
    plan = _plan(device_id, plan_id=plan_id, execute_at=initial + timedelta(hours=1))
    validated = plan_service.validate(plan)
    await scheduler.schedule(validated)

    results = await scheduler.run_due()
    assert results == []
    assert adapter.calls == []

    clock.set(initial + timedelta(hours=1, seconds=1))
    results = await scheduler.run_due()

    assert results == [{"plan_id": plan_id, "outcome": "executed"}]
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_completed_plan_reconciles_before_missed_without_replay(tmp_path) -> None:
    adapter, plan_service, scheduler, repository, audit = await _build_scheduler(
        tmp_path, grace_window=timedelta(minutes=1), durable=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan_id = "reconcile-completed-1"
    due_at = datetime.now(UTC) - timedelta(minutes=10)
    validated = plan_service.validate(_plan(device_id, plan_id=plan_id, execute_at=due_at))
    await scheduler.schedule(validated)
    await scheduler.executor.execute(validated)
    physical_calls = len(adapter.calls)

    results = await scheduler.run_due(now=datetime.now(UTC))

    assert results == [{"plan_id": plan_id, "outcome": "reconciled"}]
    assert len(adapter.calls) == physical_calls
    _, status = await repository.get(plan_id)
    assert status == "executed"
    assert not any(event.event_type == "schedule_missed" for event in audit.events)
    assert any(event.event_type == "schedule_execution_reconciled" for event in audit.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_status", "schedule_status", "outcome"),
    [
        (PlanStatus.COMPLETED, "executed", "reconciled"),
        (PlanStatus.FAILED, "failed", "reconciled"),
        (PlanStatus.PARTIALLY_FAILED, "failed", "reconciled"),
        (PlanStatus.UNKNOWN, "unknown", "reconciled"),
        (PlanStatus.CANCELLED, "cancelled", "reconciled"),
        (PlanStatus.EXECUTING, "unknown", "reconciled"),
    ],
)
async def test_terminal_plan_status_controls_schedule_reconciliation(
    tmp_path, plan_status: PlanStatus, schedule_status: str | None, outcome: str
) -> None:
    adapter, plan_service, scheduler, repository, _audit = await _build_scheduler(
        tmp_path, durable=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan_id = f"reconcile-{plan_status.value}"
    validated = plan_service.validate(
        _plan(device_id, plan_id=plan_id, execute_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await scheduler.schedule(validated)
    await scheduler.executor.plan_repository.save(
        validated.model_copy(update={"status": plan_status})
    )

    results = await scheduler.run_due()

    assert results == [{"plan_id": plan_id, "outcome": outcome}]
    assert adapter.calls == []
    stored = await repository.get(plan_id)
    assert stored is not None
    assert stored[1] == schedule_status if schedule_status is not None else stored[1] == "pending"


@pytest.mark.asyncio
async def test_schedule_transition_failure_reconciles_without_second_physical_call(
    tmp_path,
) -> None:
    adapter, plan_service, scheduler, repository, _audit = await _build_scheduler(
        tmp_path, durable=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan_id = "reconcile-transition-failure-1"
    validated = plan_service.validate(
        _plan(device_id, plan_id=plan_id, execute_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await scheduler.schedule(validated)
    original_mark_executed = repository.mark_executed
    failed_once = True

    async def fail_before_transition(identifier: str) -> bool:
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise RuntimeError("simulated schedule persistence failure")
        return await original_mark_executed(identifier)

    repository.mark_executed = fail_before_transition  # type: ignore[method-assign]
    first_results = await scheduler.run_due()

    assert first_results == [{"plan_id": plan_id, "outcome": "reconciled"}]
    assert len(adapter.calls) == 1
    assert (await repository.get(plan_id))[1] == "executed"


@pytest.mark.asyncio
async def test_reconciliation_failure_does_not_mark_terminal_plan_missed(tmp_path) -> None:
    _adapter, plan_service, scheduler, repository, _audit = await _build_scheduler(
        tmp_path, grace_window=timedelta(minutes=1), durable=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    plan_id = "reconcile-write-failure-1"
    due_at = datetime.now(UTC) - timedelta(minutes=10)
    validated = plan_service.validate(_plan(device_id, plan_id=plan_id, execute_at=due_at))
    await scheduler.schedule(validated)
    await scheduler.executor.plan_repository.save(
        validated.model_copy(update={"status": PlanStatus.COMPLETED})
    )

    async def fail_reconciliation(identifier: str, status: str) -> bool:
        raise RuntimeError(f"simulated reconciliation failure for {identifier}:{status}")

    repository.reconcile_terminal = fail_reconciliation  # type: ignore[method-assign]

    results = await scheduler.run_due(now=datetime.now(UTC))

    assert results == [{"plan_id": plan_id, "outcome": "error"}]
    assert (await repository.get(plan_id))[1] == "pending"


@pytest.mark.asyncio
async def test_recurring_cursor_failure_reconciles_without_replay(tmp_path) -> None:
    adapter, plan_service, scheduler, _repository, _audit = await _build_scheduler(
        tmp_path, durable=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    schedule_id = "reconcile-recurring-1"
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    await scheduler.recurring_repository.create(
        schedule_id, [_command(device_id, plan_id=schedule_id)], rule, due_at
    )
    original_advance = scheduler.recurring_repository.advance
    failed_once = True

    async def fail_before_advance(identifier: str, next_execute_at: datetime) -> None:
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise RuntimeError("simulated recurring persistence failure")
        await original_advance(identifier, next_execute_at)

    scheduler.recurring_repository.advance = fail_before_advance  # type: ignore[method-assign]
    first_results = await scheduler.run_due_recurring()
    second_results = await scheduler.run_due_recurring(now=due_at + timedelta(seconds=1))

    assert first_results == [{"schedule_id": schedule_id, "outcome": "error"}]
    assert second_results == [{"schedule_id": schedule_id, "outcome": "reconciled"}]
    assert len(adapter.calls) == 1
    active = await scheduler.list_recurring()
    assert active[0][3] > due_at + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_recurring_cursor_failure_does_not_abandon_other_schedule(tmp_path) -> None:
    adapter, plan_service, scheduler, _repository, _audit = await _build_scheduler(
        tmp_path, durable=True
    )
    device_id = next(
        device.id for device in plan_service.registry.devices if device.type.value == "light"
    )
    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    for schedule_id in ("reconcile-recurring-a", "reconcile-recurring-b"):
        await scheduler.recurring_repository.create(
            schedule_id, [_command(device_id, plan_id=schedule_id)], rule, due_at
        )
    original_advance = scheduler.recurring_repository.advance
    failed_once = True

    async def fail_first_schedule(identifier: str, next_execute_at: datetime) -> None:
        nonlocal failed_once
        if identifier == "reconcile-recurring-a" and failed_once:
            failed_once = False
            raise RuntimeError("simulated recurring persistence failure")
        await original_advance(identifier, next_execute_at)

    scheduler.recurring_repository.advance = fail_first_schedule  # type: ignore[method-assign]

    results = await scheduler.run_due_recurring()

    outcomes = {entry["schedule_id"]: entry["outcome"] for entry in results}
    assert outcomes == {
        "reconcile-recurring-a": "error",
        "reconcile-recurring-b": "executed",
    }
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_alive_is_false_before_run_true_while_running_false_after_cancel(
    tmp_path: Path,
) -> None:
    _, _, scheduler, _, _ = await _build_scheduler(tmp_path)
    assert scheduler.alive is False

    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0)
    assert scheduler.alive is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduler.alive is False
