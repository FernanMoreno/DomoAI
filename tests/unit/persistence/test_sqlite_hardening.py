from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from domoai.persistence.sqlite import SQLiteDatabase


@pytest.mark.asyncio
async def test_journal_mode_is_wal_after_initialize(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()

    cursor = database.connection.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    cursor.close()

    assert mode.lower() == "wal"


@pytest.mark.asyncio
async def test_busy_timeout_defaults_to_five_thousand_ms(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()

    cursor = database.connection.execute("PRAGMA busy_timeout")
    timeout = cursor.fetchone()[0]
    cursor.close()

    assert timeout == 5000


@pytest.mark.asyncio
async def test_busy_timeout_is_configurable(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3", busy_timeout_ms=250)
    await database.initialize()

    cursor = database.connection.execute("PRAGMA busy_timeout")
    timeout = cursor.fetchone()[0]
    cursor.close()

    assert timeout == 250


@pytest.mark.asyncio
async def test_mid_script_migration_failure_leaves_no_partial_schema_change(
    tmp_path: Path,
) -> None:
    """A failure injection test for the DDL/ledger atomicity gap.

    The second statement genuinely fails (not the "duplicate column
    name" idempotency case) -- this simulates a crash/error occurring
    mid-script, after the ALTER TABLE already ran but before the script
    (and therefore the ledger entry) completes, matching
    006_plan_status_column.sql's ALTER+UPDATE shape exactly. Without
    atomicity, the ALTER survives even though the migration as a whole
    never completed and was never recorded in the ledger.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_widgets.sql").write_text(
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);"
    )
    (migrations_dir / "002_widgets_status.sql").write_text(
        "ALTER TABLE widgets ADD COLUMN status TEXT NOT NULL DEFAULT '';\n"
        "UPDATE widgets SET status = nonexistent_backfill_function();\n"
    )
    db_path = tmp_path / "atomic.sqlite3"

    database = SQLiteDatabase(db_path)
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        await database.initialize(migrations_dir=migrations_dir)
    await database.close()

    verify = sqlite3.connect(db_path)
    columns = [row[1] for row in verify.execute("PRAGMA table_info(widgets)")]
    ledger = {row[0] for row in verify.execute("SELECT filename FROM schema_migrations")}
    verify.close()

    assert "status" not in columns
    assert "002_widgets_status.sql" not in ledger


@pytest.mark.asyncio
async def test_interrupted_migration_recovers_fully_on_restart(tmp_path: Path) -> None:
    """A crash strictly after a migration script fully succeeds, but
    before its ledger entry commits, must still recover cleanly on
    restart -- the already-applied data is not re-corrupted, and the
    ledger ends up correctly recording the migration.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_widgets.sql").write_text(
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);"
    )
    db_path = tmp_path / "atomic.sqlite3"

    database = SQLiteDatabase(db_path)
    await database.initialize(migrations_dir=migrations_dir)
    database.connection.execute("INSERT INTO widgets (id, payload) VALUES (1, 'hello')")
    database.connection.commit()
    await database.close()

    (migrations_dir / "002_widgets_status.sql").write_text(
        "ALTER TABLE widgets ADD COLUMN status TEXT NOT NULL DEFAULT '';\n"
        "UPDATE widgets SET status = 'backfilled';\n"
    )

    interrupted = SQLiteDatabase(db_path)
    with patch("domoai.persistence.sqlite.datetime") as mock_datetime:
        mock_datetime.now.side_effect = RuntimeError("simulated process crash mid-migration")
        with pytest.raises(RuntimeError, match="simulated process crash"):
            await interrupted.initialize(migrations_dir=migrations_dir)
    await interrupted.close()

    recovered = SQLiteDatabase(db_path)
    await recovered.initialize(migrations_dir=migrations_dir)

    row = recovered.connection.execute("SELECT status FROM widgets WHERE id = 1").fetchone()
    ledger = {
        entry[0] for entry in recovered.connection.execute("SELECT filename FROM schema_migrations")
    }
    await recovered.close()

    assert row is not None
    assert row[0] == "backfilled"
    assert "002_widgets_status.sql" in ledger


@pytest.mark.asyncio
async def test_operation_count_increments_per_execute_call(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    baseline = database.metrics.operation_count

    database.connection.execute("SELECT 1")
    database.connection.execute("SELECT 2")

    assert database.metrics.operation_count == baseline + 2


@pytest.mark.asyncio
async def test_busy_count_increments_on_database_locked_and_error_still_propagates(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    baseline_busy = database.metrics.busy_count
    baseline_ops = database.metrics.operation_count

    class _LockedConnection:
        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            raise sqlite3.OperationalError("database is locked")

    real_connection = database._connection
    database._connection = _LockedConnection()  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            database.connection.execute("SELECT 1")
    finally:
        database._connection = real_connection

    assert database.metrics.busy_count == baseline_busy + 1
    assert database.metrics.operation_count == baseline_ops + 1


@pytest.mark.asyncio
async def test_metrics_property_returns_a_copy_not_the_live_object(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()

    snapshot = database.metrics
    snapshot.operation_count = 999

    assert database.metrics.operation_count != 999
