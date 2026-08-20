from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.domain.models import Command, Plan
from domoai.persistence.repositories import PlanRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import MIGRATIONS_DIR, SQLiteDatabase


def _migration_filenames() -> list[str]:
    return sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))


@pytest.mark.asyncio
async def test_fresh_database_applies_and_records_every_migration(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()

    rows = database.connection.execute(
        "SELECT filename FROM schema_migrations ORDER BY filename"
    ).fetchall()
    assert [row[0] for row in rows] == _migration_filenames()


@pytest.mark.asyncio
async def test_reinitializing_an_already_migrated_database_is_not_reapplied(tmp_path) -> None:
    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize()
    first_count = database.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[
        0
    ]

    second = SQLiteDatabase(db_path)
    await second.initialize()
    second_count = second.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

    assert second_count == first_count == len(_migration_filenames())


@pytest.mark.asyncio
async def test_only_not_yet_applied_migrations_run_on_a_partially_migrated_database(
    tmp_path,
) -> None:
    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize()

    # Simulate an older deployment that only ever had 001_initial.sql applied:
    # forget every later migration from the ledger without dropping their tables.
    database.connection.execute("DELETE FROM schema_migrations WHERE filename != '001_initial.sql'")
    database.connection.commit()

    reopened = SQLiteDatabase(db_path)
    await reopened.initialize()

    rows = reopened.connection.execute(
        "SELECT filename FROM schema_migrations ORDER BY filename"
    ).fetchall()
    assert [row[0] for row in rows] == _migration_filenames()


@pytest.mark.asyncio
async def test_upgrade_preserves_existing_data_and_enables_new_tables(tmp_path) -> None:
    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize()

    # Simulate a database created under an older deployment: only
    # 001_initial.sql was ever recorded as applied (even though the fixture
    # already created every table, matching a real accidentally-idempotent
    # legacy database per the spec's own Assumptions).
    database.connection.execute("DELETE FROM schema_migrations WHERE filename != '001_initial.sql'")
    database.connection.commit()

    plan_repository = PlanRepository(database)
    original_plan = Plan(
        id="plan-upgrade-1",
        commands=[
            Command(
                id="command-upgrade-1",
                device_id="garden.garden-pump",
                command="turn_on",
                idempotency_key="intent-upgrade-1",
            )
        ],
    )
    await plan_repository.save(original_plan)

    upgraded = SQLiteDatabase(db_path)
    await upgraded.initialize()

    reloaded_plan = await PlanRepository(upgraded).get("plan-upgrade-1")
    assert reloaded_plan is not None
    assert reloaded_plan.id == original_plan.id
    assert reloaded_plan.commands[0].id == "command-upgrade-1"

    scheduled_repository = ScheduledPlanRepository(upgraded)
    scheduled_plan = Plan(
        id="plan-upgrade-scheduled-1",
        execute_at=datetime.now(UTC),
        commands=[
            Command(
                id="command-upgrade-scheduled-1",
                device_id="garden.garden-pump",
                command="turn_on",
                idempotency_key="intent-upgrade-scheduled-1",
            )
        ],
    )
    await scheduled_repository.schedule(scheduled_plan)
    result = await scheduled_repository.get("plan-upgrade-scheduled-1")
    assert result is not None
    assert result[1] == "pending"


@pytest.mark.asyncio
async def test_non_idempotent_migration_survives_a_second_initialization(tmp_path) -> None:
    migrations_dir = tmp_path / "custom_migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_create.sql").write_text(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY);", encoding="utf-8"
    )
    (migrations_dir / "002_add_column.sql").write_text(
        "ALTER TABLE widgets ADD COLUMN label TEXT;", encoding="utf-8"
    )

    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize(migrations_dir=migrations_dir)

    columns_first = {row[1] for row in database.connection.execute("PRAGMA table_info(widgets)")}
    assert "label" in columns_first

    reopened = SQLiteDatabase(db_path)
    await reopened.initialize(migrations_dir=migrations_dir)

    columns_second = {row[1] for row in reopened.connection.execute("PRAGMA table_info(widgets)")}
    assert columns_second == columns_first
