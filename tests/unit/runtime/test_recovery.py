from __future__ import annotations

import pytest

from domoai.application.recovery import PlanRecoveryService
from domoai.domain.models import Command, Plan, PlanStatus
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog


def _plan(plan_id: str, status: PlanStatus) -> Plan:
    return Plan(
        id=plan_id,
        status=status,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id="garden.garden-pump",
                command="turn_on",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )


async def _build(tmp_path) -> tuple[PlanRepository, AuditLog, PlanRecoveryService]:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    audit = AuditLog()
    service = PlanRecoveryService(plan_repository, audit)
    return plan_repository, audit, service


@pytest.mark.asyncio
async def test_single_executing_plan_is_recovered_to_unknown(tmp_path) -> None:
    plan_repository, _audit, service = await _build(tmp_path)
    await plan_repository.save(_plan("plan-a", PlanStatus.EXECUTING))

    recovered_ids = await service.recover_orphaned_plans()

    assert recovered_ids == ["plan-a"]
    persisted = await plan_repository.get("plan-a")
    assert persisted is not None
    assert persisted.status is PlanStatus.UNKNOWN


@pytest.mark.asyncio
async def test_recovery_emits_one_audit_event_per_plan(tmp_path) -> None:
    plan_repository, audit, service = await _build(tmp_path)
    await plan_repository.save(_plan("plan-a", PlanStatus.EXECUTING))

    await service.recover_orphaned_plans()

    recovery_events = [
        event for event in audit.events if event.event_type == "plan_execution_recovered"
    ]
    assert len(recovery_events) == 1
    assert recovery_events[0].subject_id == "plan-a"
    assert recovery_events[0].payload["reason"] == "startup_crash_recovery"
    assert recovery_events[0].payload["previous_status"] == "executing"


@pytest.mark.asyncio
async def test_multiple_executing_plans_are_all_recovered(tmp_path) -> None:
    plan_repository, audit, service = await _build(tmp_path)
    await plan_repository.save(_plan("plan-a", PlanStatus.EXECUTING))
    await plan_repository.save(_plan("plan-b", PlanStatus.EXECUTING))
    await plan_repository.save(_plan("plan-c", PlanStatus.EXECUTING))

    recovered_ids = await service.recover_orphaned_plans()

    assert set(recovered_ids) == {"plan-a", "plan-b", "plan-c"}
    for plan_id in ("plan-a", "plan-b", "plan-c"):
        persisted = await plan_repository.get(plan_id)
        assert persisted is not None
        assert persisted.status is PlanStatus.UNKNOWN
    recovery_events = [
        event for event in audit.events if event.event_type == "plan_execution_recovered"
    ]
    assert len(recovery_events) == 3


@pytest.mark.asyncio
async def test_plans_in_other_statuses_are_untouched(tmp_path) -> None:
    plan_repository, audit, service = await _build(tmp_path)
    await plan_repository.save(_plan("plan-executing", PlanStatus.EXECUTING))
    await plan_repository.save(_plan("plan-ready", PlanStatus.READY))
    await plan_repository.save(_plan("plan-completed", PlanStatus.COMPLETED))

    recovered_ids = await service.recover_orphaned_plans()

    assert recovered_ids == ["plan-executing"]
    ready = await plan_repository.get("plan-ready")
    completed = await plan_repository.get("plan-completed")
    assert ready is not None and ready.status is PlanStatus.READY
    assert completed is not None and completed.status is PlanStatus.COMPLETED
    recovery_events = [
        event for event in audit.events if event.event_type == "plan_execution_recovered"
    ]
    assert {event.subject_id for event in recovery_events} == {"plan-executing"}


@pytest.mark.asyncio
async def test_empty_store_is_a_no_op(tmp_path) -> None:
    _plan_repository, audit, service = await _build(tmp_path)

    recovered_ids = await service.recover_orphaned_plans()

    assert recovered_ids == []
    assert audit.events == ()


@pytest.mark.asyncio
async def test_second_call_after_recovery_is_idempotent(tmp_path) -> None:
    plan_repository, audit, service = await _build(tmp_path)
    await plan_repository.save(_plan("plan-a", PlanStatus.EXECUTING))
    await service.recover_orphaned_plans()

    second_recovered_ids = await service.recover_orphaned_plans()

    assert second_recovered_ids == []
    recovery_events = [
        event for event in audit.events if event.event_type == "plan_execution_recovered"
    ]
    assert len(recovery_events) == 1
