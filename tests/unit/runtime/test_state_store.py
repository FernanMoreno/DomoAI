from datetime import UTC, datetime

import pytest

from domoai.domain.models import SourceRef, StateSnapshot, StateStatus
from domoai.runtime.state_store import StateStore


def _snapshot(value: object, *, status: StateStatus = StateStatus.CURRENT) -> StateSnapshot:
    return StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=value,
        observed_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        status=status,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
    )


@pytest.mark.asyncio
async def test_state_version_starts_at_zero_for_unknown_key() -> None:
    store = StateStore()

    assert store.state_version("light.kitchen", "brightness") == 0


@pytest.mark.asyncio
async def test_first_save_advances_state_version() -> None:
    store = StateStore()

    await store.save(_snapshot(50))

    assert store.state_version("light.kitchen", "brightness") > 0


@pytest.mark.asyncio
async def test_resaving_identical_value_does_not_advance_version() -> None:
    store = StateStore()
    await store.save(_snapshot(50))
    version_after_first_save = store.state_version("light.kitchen", "brightness")

    await store.save(_snapshot(50))

    assert store.state_version("light.kitchen", "brightness") == version_after_first_save


@pytest.mark.asyncio
async def test_changed_value_advances_version() -> None:
    store = StateStore()
    await store.save(_snapshot(50))
    version_after_first_save = store.state_version("light.kitchen", "brightness")

    await store.save(_snapshot(75))

    assert store.state_version("light.kitchen", "brightness") > version_after_first_save


@pytest.mark.asyncio
async def test_load_persisted_forces_stale_status() -> None:
    store = StateStore()
    current = StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=50,
        observed_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
    )

    store.load_persisted([current])

    restored = await store.get("light.kitchen", "brightness")
    assert restored is not None
    assert restored.status is StateStatus.STALE


@pytest.mark.asyncio
async def test_load_persisted_seeds_state_version() -> None:
    store = StateStore()

    store.load_persisted([_snapshot(50)])

    assert store.state_version("light.kitchen", "brightness") > 0


@pytest.mark.asyncio
async def test_changed_status_advances_version_even_with_same_value() -> None:
    store = StateStore()
    await store.save(_snapshot(50, status=StateStatus.CURRENT))
    version_after_first_save = store.state_version("light.kitchen", "brightness")

    await store.save(_snapshot(50, status=StateStatus.STALE))

    assert store.state_version("light.kitchen", "brightness") > version_after_first_save
