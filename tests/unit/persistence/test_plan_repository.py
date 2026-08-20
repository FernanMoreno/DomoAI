from __future__ import annotations

import pytest

from domoai.domain.models import Command, Plan, PlanStatus
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase


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


@pytest.mark.asyncio
async def test_list_by_status_returns_only_matching_plans(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    executing = _plan("plan-executing", PlanStatus.EXECUTING)
    ready = _plan("plan-ready", PlanStatus.READY)
    await repository.save(executing)
    await repository.save(ready)

    result = await repository.list_by_status(frozenset({PlanStatus.EXECUTING}))

    assert [plan.id for plan in result] == ["plan-executing"]


@pytest.mark.asyncio
async def test_list_by_status_supports_multiple_statuses(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    executing = _plan("plan-executing", PlanStatus.EXECUTING)
    failed = _plan("plan-failed", PlanStatus.FAILED)
    ready = _plan("plan-ready", PlanStatus.READY)
    await repository.save(executing)
    await repository.save(failed)
    await repository.save(ready)

    result = await repository.list_by_status(frozenset({PlanStatus.EXECUTING, PlanStatus.FAILED}))

    assert {plan.id for plan in result} == {"plan-executing", "plan-failed"}


@pytest.mark.asyncio
async def test_list_by_status_on_empty_store_returns_empty_list(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)

    result = await repository.list_by_status(frozenset({PlanStatus.EXECUTING}))

    assert result == []
