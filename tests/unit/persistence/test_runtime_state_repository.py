from __future__ import annotations

import pytest

from domoai.persistence.repositories import RuntimeStateMetadataRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.state_store import StateStoreMetadata


@pytest.mark.asyncio
async def test_runtime_state_metadata_round_trips_across_database_instances(tmp_path) -> None:
    database_path = tmp_path / "runtime-state.sqlite3"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    repository = RuntimeStateMetadataRepository(database)
    metadata = StateStoreMetadata(
        inventory_revision=4,
        version_counter=12,
        state_versions={("light.kitchen", "brightness"): 12},
        inventory_fingerprint="sha256:inventory",
    )

    await repository.save(metadata)
    await database.close()

    restarted_database = SQLiteDatabase(database_path)
    await restarted_database.initialize()
    restored = await RuntimeStateMetadataRepository(restarted_database).get()

    assert restored == metadata


@pytest.mark.asyncio
async def test_runtime_state_metadata_is_absent_for_legacy_database(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "legacy.sqlite3")
    await database.initialize()

    assert await RuntimeStateMetadataRepository(database).get() is None


@pytest.mark.asyncio
async def test_malformed_runtime_state_metadata_falls_back_conservatively(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "malformed.sqlite3")
    await database.initialize()
    database.connection.execute(
        "INSERT INTO runtime_state_metadata (id, payload, updated_at) VALUES (1, ?, ?)",
        ("not-json", "2026-08-21T00:00:00+00:00"),
    )
    database.connection.commit()

    assert await RuntimeStateMetadataRepository(database).get() is None


@pytest.mark.asyncio
async def test_runtime_state_metadata_replaces_singleton_row(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime-state.sqlite3")
    await database.initialize()
    repository = RuntimeStateMetadataRepository(database)

    await repository.save(
        StateStoreMetadata(
            inventory_revision=1,
            version_counter=2,
            state_versions={("switch.pump", "power"): 2},
        )
    )
    updated = StateStoreMetadata(
        inventory_revision=2,
        version_counter=3,
        state_versions={("switch.pump", "power"): 3},
    )
    await repository.save(updated)

    assert await repository.get() == updated
