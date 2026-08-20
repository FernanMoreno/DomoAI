"""Confirms every repository writing `updated_at` honors an injected Clock (Spec 082)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.domain.models import Command, Plan, RecurrenceRule
from domoai.persistence.repositories import (
    DeviceRepository,
    PlanRepository,
    RecurringScheduleRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock

FIXED = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))


async def _database(tmp_path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    return database


def _fetch_updated_at(database: SQLiteDatabase, table: str, id_column: str, id_value: str) -> str:
    cursor = database.connection.execute(
        f"SELECT updated_at FROM {table} WHERE {id_column} = ?", (id_value,)
    )
    row = cursor.fetchone()
    cursor.close()
    assert row is not None
    return str(row[0])


@pytest.mark.asyncio
async def test_device_repository_stamps_updated_at_from_the_injected_clock(
    tmp_path: Path,
) -> None:
    from domoai.domain.models import Capability, CapabilityKind, Device, DeviceType, SourceRef

    database = await _database(tmp_path)
    repository = DeviceRepository(database, clock=FIXED)
    device = Device(
        id="light.clock-test",
        name="Clock Test Light",
        type=DeviceType.LIGHT,
        protocol="fixture",
        capabilities=[
            Capability(
                name="power",
                kind=CapabilityKind.BOOLEAN,
                readable=True,
                writable=True,
                commands=["turn_on"],
            )
        ],
        area_id="living_room",
        source_refs=[SourceRef(adapter_id="fixture", external_id="light.clock-test")],
    )

    await repository.save(device)

    assert (
        _fetch_updated_at(database, "devices", "id", "light.clock-test") == FIXED.now().isoformat()
    )


@pytest.mark.asyncio
async def test_plan_repository_save_stamps_updated_at_from_the_injected_clock(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    repository = PlanRepository(database, clock=FIXED)
    plan = Plan(
        id="plan-clock-test",
        commands=[
            Command(
                id="cmd-1",
                device_id="light.x",
                command="turn_on",
                idempotency_key="intent-clock-test",
            )
        ],
    )

    await repository.save(plan)

    assert _fetch_updated_at(database, "plans", "id", "plan-clock-test") == FIXED.now().isoformat()


@pytest.mark.asyncio
async def test_plan_repository_claim_for_execution_stamps_updated_at_from_the_injected_clock(
    tmp_path: Path,
) -> None:
    from domoai.domain.models import PlanStatus

    database = await _database(tmp_path)
    repository = PlanRepository(database, clock=FIXED)
    plan = Plan(
        id="plan-claim-clock-test",
        commands=[
            Command(
                id="cmd-1",
                device_id="light.x",
                command="turn_on",
                idempotency_key="intent-claim-clock-test",
            )
        ],
    )
    await repository.save(plan)

    claimed = await repository.claim_for_execution(
        plan.model_copy(update={"status": PlanStatus.EXECUTING}),
        allowed_statuses=frozenset({PlanStatus.DRAFT}),
    )

    assert claimed is True
    assert (
        _fetch_updated_at(database, "plans", "id", "plan-claim-clock-test")
        == FIXED.now().isoformat()
    )


@pytest.mark.asyncio
async def test_scheduled_plan_repository_schedule_stamps_updated_at_from_the_injected_clock(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    repository = ScheduledPlanRepository(database, clock=FIXED)
    plan = Plan(
        id="plan-scheduled-clock-test",
        execute_at=datetime(2030, 1, 1, tzinfo=UTC),
        commands=[
            Command(
                id="cmd-1",
                device_id="light.x",
                command="turn_on",
                idempotency_key="intent-scheduled-clock-test",
            )
        ],
    )

    await repository.schedule(plan)

    assert (
        _fetch_updated_at(database, "scheduled_plans", "plan_id", "plan-scheduled-clock-test")
        == FIXED.now().isoformat()
    )


@pytest.mark.asyncio
async def test_scheduled_plan_repository_transition_stamps_updated_at_from_the_injected_clock(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    repository = ScheduledPlanRepository(database, clock=FIXED)
    plan = Plan(
        id="plan-transition-clock-test",
        execute_at=datetime(2030, 1, 1, tzinfo=UTC),
        commands=[
            Command(
                id="cmd-1",
                device_id="light.x",
                command="turn_on",
                idempotency_key="intent-transition-clock-test",
            )
        ],
    )
    await repository.schedule(plan)

    cancelled = await repository.cancel("plan-transition-clock-test")

    assert cancelled is True
    assert (
        _fetch_updated_at(database, "scheduled_plans", "plan_id", "plan-transition-clock-test")
        == FIXED.now().isoformat()
    )


@pytest.mark.asyncio
async def test_recurring_schedule_repository_create_stamps_updated_at_from_the_injected_clock(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    repository = RecurringScheduleRepository(database, clock=FIXED)
    commands = [
        Command(
            id="cmd-1",
            device_id="light.x",
            command="turn_on",
            idempotency_key="intent-recurring-clock-test",
        )
    ]
    from datetime import time

    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")

    await repository.create("schedule-clock-test", commands, rule, datetime(2030, 1, 1, tzinfo=UTC))

    assert (
        _fetch_updated_at(database, "recurring_schedules", "schedule_id", "schedule-clock-test")
        == FIXED.now().isoformat()
    )


@pytest.mark.asyncio
async def test_recurring_schedule_repository_advance_stamps_updated_at_from_the_injected_clock(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    repository = RecurringScheduleRepository(database, clock=FIXED)
    commands = [
        Command(
            id="cmd-1",
            device_id="light.x",
            command="turn_on",
            idempotency_key="intent-recurring-advance-clock-test",
        )
    ]
    from datetime import time

    rule = RecurrenceRule(time_of_day=time(0, 0), timezone="UTC")
    await repository.create(
        "schedule-advance-clock-test", commands, rule, datetime(2030, 1, 1, tzinfo=UTC)
    )

    await repository.advance("schedule-advance-clock-test", datetime(2030, 1, 2, tzinfo=UTC))

    assert (
        _fetch_updated_at(
            database, "recurring_schedules", "schedule_id", "schedule-advance-clock-test"
        )
        == FIXED.now().isoformat()
    )
