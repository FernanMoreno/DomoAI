from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.domain.models import (
    AvailabilityStatus,
    Device,
    DeviceType,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.persistence.repositories import DeviceRepository, StateSnapshotRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _device(device_id: str = "light.kitchen") -> Device:
    return Device(
        id=device_id,
        type=DeviceType.LIGHT,
        name=device_id,
        protocol="fixture",
        availability=AvailabilityStatus.AVAILABLE,
        source_refs=[SourceRef(adapter_id="fixture", external_id=device_id)],
    )


def _snapshot(device_id: str = "light.kitchen") -> StateSnapshot:
    now = datetime.now(UTC)
    return StateSnapshot(
        device_id=device_id,
        capability="power",
        value=True,
        observed_at=now,
        received_at=now,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id=device_id),
    )


@pytest.mark.asyncio
async def test_device_repository_round_trips(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = DeviceRepository(database)
    device = _device()

    await repository.save(device)

    assert await repository.get(device.id) == device
    assert await repository.list_all() == [device]
    await database.close()


@pytest.mark.asyncio
async def test_device_repository_upserts(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = DeviceRepository(database)
    device = _device()
    await repository.save(device)

    updated = device.model_copy(update={"availability": AvailabilityStatus.UNAVAILABLE})
    await repository.save(updated)

    assert await repository.list_all() == [updated]
    await database.close()


@pytest.mark.asyncio
async def test_state_snapshot_repository_round_trips(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = StateSnapshotRepository(database)
    snapshot = _snapshot()

    await repository.save(snapshot)

    assert await repository.list_all() == [snapshot]
    await database.close()


@pytest.mark.asyncio
async def test_state_snapshot_repository_upserts_by_device_and_capability(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = StateSnapshotRepository(database)
    await repository.save(_snapshot())

    updated = _snapshot().model_copy(update={"value": False})
    await repository.save(updated)

    stored = await repository.list_all()
    assert len(stored) == 1
    assert stored[0].value is False
    await database.close()
