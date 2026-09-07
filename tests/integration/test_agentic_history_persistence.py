from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.models import SourceRef, StateSnapshot, StateStatus
from domoai.persistence.repositories import (
    RuntimeStatePersistenceRepository,
    StateHistoryRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock
from domoai.runtime.state_store import StateStoreMetadata


def _snapshot(
    device_id: str,
    value: int,
    received_at: datetime,
    *,
    capability: str = "brightness",
) -> StateSnapshot:
    return StateSnapshot(
        device_id=device_id,
        capability=capability,
        value=value,
        observed_at=received_at,
        received_at=received_at,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id=device_id),
    )


@pytest.mark.asyncio
async def test_history_is_scoped_filtered_ordered_and_retained(tmp_path) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    database = SQLiteDatabase(tmp_path / "history.sqlite3")
    await database.initialize()
    try:
        repository = StateHistoryRepository(
            database,
            household_id="home-a",
            retention_days=1,
            clock=FixedClock(now),
        )
        await repository.append(
            [
                _snapshot("light.kitchen", 10, now - timedelta(hours=2)),
                _snapshot("light.kitchen", 20, now - timedelta(hours=1)),
                _snapshot("sensor.office", 30, now - timedelta(hours=1)),
                _snapshot("light.kitchen", 99, now - timedelta(days=2)),
            ]
        )
        await repository.append([_snapshot("light.kitchen", 20, now - timedelta(hours=1))])

        records = await repository.list_history(
            device_ids=("light.kitchen",),
            capabilities=("brightness",),
            start=now - timedelta(hours=3),
            end=now,
            limit=50,
        )

        assert [record.snapshot.value for record in records] == [20, 10]
        assert len({record.history_id for record in records}) == 2
        assert all(record.snapshot.device_id == "light.kitchen" for record in records)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_history_repository_cannot_cross_households(tmp_path) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    database = SQLiteDatabase(tmp_path / "history-scope.sqlite3")
    await database.initialize()
    try:
        home_a = StateHistoryRepository(database, household_id="home-a", clock=FixedClock(now))
        home_b = StateHistoryRepository(database, household_id="home-b", clock=FixedClock(now))
        sample = _snapshot("light.kitchen", 10, now)
        await home_a.append([sample])
        await home_b.append([_snapshot("light.kitchen", 20, now)])

        assert [record.snapshot.value for record in await home_a.list_history()] == [10]
        assert [record.snapshot.value for record in await home_b.list_history()] == [20]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_runtime_state_and_history_commit_together(tmp_path) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    database = SQLiteDatabase(tmp_path / "history-atomic.sqlite3")
    await database.initialize()
    try:
        history = StateHistoryRepository(database, household_id="home-a", clock=FixedClock(now))
        persistence = RuntimeStatePersistenceRepository(
            database,
            clock=FixedClock(now),
            history_repository=history,
        )
        snapshot = _snapshot("light.kitchen", 42, now)

        await persistence.persist(
            [snapshot],
            StateStoreMetadata(
                inventory_revision=0,
                version_counter=1,
                state_versions={(snapshot.device_id, snapshot.capability): 1},
            ),
        )

        assert (
            database.connection.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0]
            == 1
        )
        assert database.connection.execute("SELECT COUNT(*) FROM state_history").fetchone()[0] == 1
    finally:
        await database.close()
