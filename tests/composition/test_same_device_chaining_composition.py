from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan, Precondition
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


@pytest.mark.composition
@pytest.mark.asyncio
async def test_real_bundle_scheduler_executor_composes_same_device_slots(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = SimulatedHomeAdapter(clock=clock)
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")

    database = SQLiteDatabase(tmp_path / "same-device-chain.sqlite3", clock=clock)
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

    first = plan_service.validate(
        Plan(
            id="same-device-slot-1",
            execute_at=now + timedelta(seconds=1),
            commands=[
                Command(
                    id="same-device-command-1",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="same-device-intent-1",
                )
            ],
        )
    )
    second = plan_service.validate(
        Plan(
            id="same-device-slot-2",
            execute_at=now + timedelta(seconds=2),
            commands=[
                Command(
                    id="same-device-command-2",
                    device_id=light_id,
                    command="set_brightness",
                    value=20,
                    idempotency_key="same-device-intent-2",
                    preconditions=[
                        Precondition(device_id=light_id, capability="power", expected=True)
                    ],
                )
            ],
        )
    )
    for plan in (first, second):
        await plan_repository.save_validation(plan)

    members = [
        BundleCommitRequestMember(
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            execute_at=plan.execute_at,
            predecessor_plan_id=first.id if plan.id == second.id else None,
        )
        for plan in (first, second)
    ]
    request = BundleCommitRequest(
        bundle_digest=bundle_approval_digest("same-device-scenario", members),
        scenario_id="same-device-scenario",
        members=members,
    )

    committed = await bundle_service.commit(request)
    assert committed.status.value == "scheduled"

    clock.set(now + timedelta(seconds=3))
    results = await scheduler.run_due()

    assert [item["outcome"] for item in results] == ["executed", "executed"]
    assert len(adapter.calls) == 2
    assert [call.command for call in adapter.calls] == ["turn_on", "set_brightness"]
    persisted_first = await plan_repository.get(first.id)
    persisted_second = await plan_repository.get(second.id)
    assert persisted_first is not None and persisted_first.status.value == "completed"
    assert persisted_second is not None and persisted_second.status.value == "completed"
    final_state = await state_store.get(light_id, "brightness")
    assert final_state is not None and final_state.value == 20
    bundle = await bundle_repository.get(committed.id)
    assert bundle is not None
    assert bundle.status.value == "completed"
    assert bundle.members[0].status.value == "executed"
    assert bundle.members[1].status.value == "executed"
    assert "dependency_evidence" in bundle.members[0].details
