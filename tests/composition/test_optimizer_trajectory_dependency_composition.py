"""Optimizer -> Skill -> BundleCommit -> Scheduler physical-trajectory gate.

Closes the P0 finding from the 2026-08-23 re-audit of commit 61439f3
("harden cross-system execution"): a scheduled bundle member with a
``predecessor_plan_id`` is a physical trajectory the optimizer computed
*assuming* its predecessor happened (e.g. "charge the battery at 10:00,
then discharge at 11:00"). Before this fix, ``Scheduler.run_due`` used the
predecessor purely to advance state-version overrides -- if the predecessor
never reached ``confirmed_success`` the dependent command still dispatched
to the adapter, unconditionally.

This exercises the real SQLite-backed BundleCommitService + Scheduler +
PlanExecutor + SimulatedHomeAdapter stack (same harness as
``test_same_device_chaining_composition.py``, which proves the success
path), against the two failure trajectories the audit called out:
predecessor FAILED/REJECTED, and predecessor MISSED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan
from domoai.optimizer.scenario import Constraint, Horizon, Load, OptimizationScenario
from domoai.persistence.repositories import (
    BundleCommitRepository,
    PlanRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.bundle_commit import (
    BundleCommitRequest,
    BundleCommitRequestMember,
    BundleCommitService,
    bundle_approval_digest,
)
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore
from domoai.skills.validator import V3_OPERATION_BINDINGS
from domoai.skills.workflow import EnergySkillRequest, EnergySkillWorkflow, WorkflowStatus
from tests.fixtures.skill_workflow import (
    build_workflow_fixture,
    light_device_id,
)


class _CountingAdapter(SimulatedHomeAdapter):
    """Records every dispatch attempt, accepted or rejected."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.attempted: list[Command] = []

    async def execute(self, command, execution_context=None):  # type: ignore[override]
        self.attempted.append(command)
        return await super().execute(command, execution_context)


async def _build_harness(tmp_path, now: datetime, *, grace_window: timedelta):
    clock = FixedClock(now)
    adapter = _CountingAdapter(clock=clock)
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")

    database = SQLiteDatabase(tmp_path / "trajectory-dependency.sqlite3", clock=clock)
    await database.initialize()
    plan_repository = PlanRepository(database, clock=clock)
    scheduled_repository = ScheduledPlanRepository(database, clock=clock)
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    facade = DomoticsFacade(plan_service, executor)
    bundle_repository = BundleCommitRepository(database, clock=clock)
    scheduler = Scheduler(
        executor,
        scheduled_repository,
        audit,
        bundle_repository=bundle_repository,
        grace_window=grace_window,
        clock=clock,
    )
    bundle_service = BundleCommitService(
        facade=facade,
        plans={},
        approval_store=ApprovalStore(operator_token="operator", clock=clock),
        bundle_repository=bundle_repository,
        scheduled_repository=scheduled_repository,
        audit=audit,
        plan_repository=plan_repository,
        clock=clock,
    )
    return (
        adapter,
        device_id,
        plan_repository,
        plan_service,
        scheduler,
        bundle_service,
        bundle_repository,
        clock,
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_dependent_battery_action_never_dispatches_after_predecessor_rejected(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    (
        adapter,
        device_id,
        plan_repository,
        plan_service,
        scheduler,
        bundle_service,
        bundle_repository,
        clock,
    ) = await _build_harness(tmp_path, now, grace_window=timedelta(hours=1))

    charge = plan_service.validate(
        Plan(
            id="battery-plan-0",
            execute_at=now + timedelta(minutes=1),
            commands=[
                Command(
                    id="battery-charge",
                    device_id=device_id,
                    command="turn_on",
                    idempotency_key="battery-charge-intent",
                )
            ],
        )
    )
    discharge = plan_service.validate(
        Plan(
            id="battery-plan-1",
            execute_at=now + timedelta(minutes=2),
            commands=[
                Command(
                    id="battery-discharge",
                    device_id=device_id,
                    command="turn_off",
                    idempotency_key="battery-discharge-intent",
                )
            ],
        )
    )
    for plan in (charge, discharge):
        await plan_repository.save_validation(plan)

    # Force the adapter to reject the charge slot deterministically -- a
    # readback/adapter failure the optimizer's SOC trajectory did not
    # foresee, exactly the P0 scenario from the audit.
    adapter._executed_idempotency_keys.add("battery-charge-intent")

    members = [
        BundleCommitRequestMember(
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            execute_at=plan.execute_at,
            predecessor_plan_id=charge.id if plan.id == discharge.id else None,
        )
        for plan in (charge, discharge)
    ]
    request = BundleCommitRequest(
        bundle_digest=bundle_approval_digest("battery-trajectory-scenario", members),
        scenario_id="battery-trajectory-scenario",
        members=members,
    )
    committed = await bundle_service.commit(request)
    assert committed.status.value == "scheduled"

    clock.set(now + timedelta(minutes=3))
    results = await scheduler.run_due()

    outcomes = {item["plan_id"]: item["outcome"] for item in results}
    assert outcomes[charge.id] not in {"missed", "dependency_failed"}
    assert outcomes[discharge.id] == "dependency_failed"

    # The hard gate: the dependent command must never reach the adapter.
    assert all(command.id != "battery-discharge" for command in adapter.attempted)

    bundle = await bundle_repository.get(committed.id)
    assert bundle is not None
    discharge_member = next(m for m in bundle.members if m.plan_id == discharge.id)
    assert discharge_member.status.value == "dependency_failed"
    assert discharge_member.details.get("reason") == "predecessor_not_confirmed_success"
    assert bundle.status.value == "failed"


@pytest.mark.composition
@pytest.mark.asyncio
async def test_dependent_battery_action_never_dispatches_after_predecessor_missed(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    (
        adapter,
        device_id,
        plan_repository,
        plan_service,
        scheduler,
        bundle_service,
        bundle_repository,
        clock,
    ) = await _build_harness(tmp_path, now, grace_window=timedelta(seconds=5))

    charge = plan_service.validate(
        Plan(
            id="battery-plan-missed-0",
            execute_at=now + timedelta(seconds=1),
            commands=[
                Command(
                    id="battery-charge-missed",
                    device_id=device_id,
                    command="turn_on",
                    idempotency_key="battery-charge-missed-intent",
                )
            ],
        )
    )
    discharge = plan_service.validate(
        Plan(
            id="battery-plan-missed-1",
            execute_at=now + timedelta(minutes=2),
            commands=[
                Command(
                    id="battery-discharge-missed",
                    device_id=device_id,
                    command="turn_off",
                    idempotency_key="battery-discharge-missed-intent",
                )
            ],
        )
    )
    for plan in (charge, discharge):
        await plan_repository.save_validation(plan)

    members = [
        BundleCommitRequestMember(
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            execute_at=plan.execute_at,
            predecessor_plan_id=charge.id if plan.id == discharge.id else None,
        )
        for plan in (charge, discharge)
    ]
    request = BundleCommitRequest(
        bundle_digest=bundle_approval_digest("battery-missed-scenario", members),
        scenario_id="battery-missed-scenario",
        members=members,
    )
    committed = await bundle_service.commit(request)
    assert committed.status.value == "scheduled"

    # First sweep: charge is overdue past its 5s grace window; discharge is
    # not due yet, so it must not even be considered.
    clock.set(now + timedelta(seconds=10))
    first_sweep = await scheduler.run_due()
    first_outcomes = {item["plan_id"]: item["outcome"] for item in first_sweep}
    assert first_outcomes[charge.id] == "missed"
    assert discharge.id not in first_outcomes
    assert adapter.attempted == []

    # Second sweep: discharge is now due (right at its own execute_at, so it
    # is not itself overdue past grace), but its predecessor was MISSED,
    # never EXECUTED -- the gate must block it just as it does for FAILED.
    clock.set(now + timedelta(minutes=2))
    second_sweep = await scheduler.run_due()
    second_outcomes = {item["plan_id"]: item["outcome"] for item in second_sweep}
    assert second_outcomes[discharge.id] == "dependency_failed"
    assert adapter.attempted == []

    bundle = await bundle_repository.get(committed.id)
    assert bundle is not None
    discharge_member = next(m for m in bundle.members if m.plan_id == discharge.id)
    assert discharge_member.status.value == "dependency_failed"


@pytest.mark.composition
@pytest.mark.asyncio
async def test_optimizer_to_skill_bundle_propagates_same_device_predecessors() -> None:
    """The production optimizer/Skill path must carry the trajectory chain."""

    clock = FixedClock(datetime.now(UTC))
    start = clock.now() + timedelta(minutes=2)
    horizon = Horizon(
        start=start,
        end=start + timedelta(minutes=4),
        resolution_minutes=1,
        timezone="Europe/Madrid",
    )
    fixture = await build_workflow_fixture(horizon=horizon, clock=clock)
    device_id = light_device_id(fixture)
    scenario = OptimizationScenario(
        id="composition-trajectory-scenario",
        horizon=horizon,
        loads=[
            Load(
                id="trajectory-load-0",
                device_id=device_id,
                capability="power",
                command="turn_on",
                value=True,
                power=100,
                power_unit="W",
                duration_slots=1,
                earliest_slot=0,
                latest_slot=0,
            ),
            Load(
                id="trajectory-load-1",
                device_id=device_id,
                capability="power",
                command="turn_on",
                value=True,
                power=100,
                power_unit="W",
                duration_slots=1,
                earliest_slot=2,
                latest_slot=2,
            ),
        ],
        constraints=[Constraint(type="max_house_power", value=500, unit="W")],
        solver_time_limit_seconds=5.0,
    )

    result = await EnergySkillWorkflow(
        fixture.router,
        fixture.approval,
        operation_bindings=V3_OPERATION_BINDINGS,
    ).run(
        EnergySkillRequest(
            scenario=scenario,
            devices=[device_id],
            capabilities=["power"],
        )
    )

    assert result.status is WorkflowStatus.SCHEDULED
    commit_call = next(
        arguments
        for provider, tool, arguments in reversed(fixture.router.calls)
        if provider == "mcp" and tool == "commit_or_schedule_bundle"
    )
    members = commit_call["members"]
    assert len(members) == 2
    assert members[0]["predecessor_plan_id"] is None
    assert members[1]["predecessor_plan_id"] == members[0]["plan_id"]


@pytest.mark.composition
@pytest.mark.asyncio
async def test_real_optimizer_failure_blocks_later_scheduled_trajectory_member() -> None:
    """A failed first CP-SAT slot must block its later scheduled slot."""

    clock = FixedClock(datetime.now(UTC))
    start = clock.now() + timedelta(minutes=2)
    horizon = Horizon(
        start=start,
        end=start + timedelta(minutes=4),
        resolution_minutes=1,
        timezone="Europe/Madrid",
    )
    fixture = await build_workflow_fixture(horizon=horizon, clock=clock)
    device_id = light_device_id(fixture)
    scenario = OptimizationScenario(
        id="composition-trajectory-failure-scenario",
        horizon=horizon,
        loads=[
            Load(
                id="failure-load-0",
                device_id=device_id,
                capability="power",
                command="turn_on",
                value=True,
                power=100,
                power_unit="W",
                duration_slots=1,
                earliest_slot=0,
                latest_slot=0,
            ),
            Load(
                id="failure-load-1",
                device_id=device_id,
                capability="power",
                command="turn_on",
                value=True,
                power=100,
                power_unit="W",
                duration_slots=1,
                earliest_slot=2,
                latest_slot=2,
            ),
        ],
        constraints=[Constraint(type="max_house_power", value=500, unit="W")],
        solver_time_limit_seconds=5.0,
    )
    result = await EnergySkillWorkflow(
        fixture.router,
        fixture.approval,
        operation_bindings=V3_OPERATION_BINDINGS,
    ).run(EnergySkillRequest(scenario=scenario, devices=[device_id], capabilities=["power"]))

    assert result.status is WorkflowStatus.SCHEDULED
    first_id, second_id = result.plan_ids
    first_plan = fixture.domotics_context.plans[first_id]
    fixture.domotics_adapter._executed_idempotency_keys.add(
        first_plan.commands[0].idempotency_key
    )

    scheduler = fixture.domotics_context.scheduler
    assert scheduler is not None
    clock.set(start + timedelta(minutes=3))
    results = await scheduler.run_due()

    outcomes = {item["plan_id"]: item["outcome"] for item in results}
    assert outcomes[first_id] == "failed"
    assert outcomes[second_id] == "dependency_failed"
    assert len(fixture.domotics_adapter.calls) == 0
