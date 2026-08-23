from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.models import Command, Plan
from domoai.persistence.repositories import ScheduledPlanRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _scheduled_plan(*, plan_id: str = "plan-scheduled-1", minutes: int = 30) -> Plan:
    return Plan(
        id=plan_id,
        execute_at=datetime.now(UTC) + timedelta(minutes=minutes),
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id="garden.garden-pump",
                command="turn_on",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )


@pytest.mark.asyncio
async def test_schedule_then_get_returns_pending(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ScheduledPlanRepository(database)
    plan = _scheduled_plan()

    await repository.schedule(plan)
    result = await repository.get(plan.id)

    assert result is not None
    stored_plan, status = result
    assert status == "pending"
    assert stored_plan.id == plan.id


@pytest.mark.asyncio
async def test_cancel_succeeds_only_while_pending(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ScheduledPlanRepository(database)
    plan = _scheduled_plan()
    await repository.schedule(plan)

    assert await repository.cancel(plan.id) is True
    assert await repository.cancel(plan.id) is False

    _, status = await repository.get(plan.id)
    assert status == "cancelled"


@pytest.mark.asyncio
async def test_reschedule_succeeds_only_while_pending(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ScheduledPlanRepository(database)
    plan = _scheduled_plan()
    await repository.schedule(plan)

    new_time = datetime.now(UTC) + timedelta(hours=2)
    assert await repository.reschedule(plan.id, new_time) is True
    stored_plan, _ = await repository.get(plan.id)
    assert stored_plan.execute_at == new_time

    await repository.mark_executed(plan.id)
    assert await repository.reschedule(plan.id, new_time) is False


@pytest.mark.asyncio
async def test_pending_schedule_survives_a_fresh_repository_instance(tmp_path) -> None:
    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize()
    repository = ScheduledPlanRepository(database)
    plan = _scheduled_plan()
    await repository.schedule(plan)

    restarted_database = SQLiteDatabase(db_path)
    await restarted_database.initialize()
    restarted_repository = ScheduledPlanRepository(restarted_database)

    pending = await restarted_repository.list_pending()
    assert [item.id for item in pending] == [plan.id]


@pytest.mark.asyncio
async def test_terminal_reconciliation_is_idempotent_and_does_not_overwrite(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ScheduledPlanRepository(database)
    plan = _scheduled_plan(plan_id="plan-reconcile-1")
    await repository.schedule(plan)

    assert await repository.reconcile_terminal(plan.id, "executed") is True
    assert await repository.reconcile_terminal(plan.id, "executed") is True
    assert await repository.reconcile_terminal(plan.id, "failed") is False

    _, status = await repository.get(plan.id)
    assert status == "executed"


@pytest.mark.asyncio
async def test_mark_executed_reports_existing_terminal_state(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ScheduledPlanRepository(database)
    plan = _scheduled_plan(plan_id="plan-reconcile-2")
    await repository.schedule(plan)

    assert await repository.mark_executed(plan.id) is True
    assert await repository.mark_executed(plan.id) is True
