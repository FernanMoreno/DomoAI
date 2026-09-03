from pathlib import Path

import pytest

from domoai.persistence.sqlite import SQLiteDatabase


@pytest.mark.asyncio
async def test_gateway_authority_migration_registers_ownership_and_approval_tables(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "gateway.sqlite3")
    await database.initialize()

    tables = {
        row[0]
        for row in database.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    migrations = {
        row[0]
        for row in database.connection.execute("SELECT filename FROM schema_migrations").fetchall()
    }

    assert {"runtime_ownership", "approval_grants"} <= tables
    assert "009_gateway_authority.sql" in migrations
    await database.close()
