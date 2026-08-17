from pathlib import Path

import pytest

from domoai.persistence.repositories import SQLiteJsonRepository
from domoai.persistence.sqlite import SQLiteDatabase


@pytest.mark.asyncio
async def test_sqlite_json_repository_round_trip(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "domoai.sqlite3")
    await database.initialize()
    try:
        repository = SQLiteJsonRepository(database, "devices")
        await repository.save("living_room.main_light", {"type": "light"})
        assert await repository.get("living_room.main_light") == {"type": "light"}
    finally:
        await database.close()
