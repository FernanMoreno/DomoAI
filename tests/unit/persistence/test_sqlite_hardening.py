from __future__ import annotations

from pathlib import Path

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
