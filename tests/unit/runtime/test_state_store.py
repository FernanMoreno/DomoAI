from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.models import SourceRef, StateSnapshot, StateStatus
from domoai.runtime.clock import FixedClock
from domoai.runtime.state_store import StateStore, StateStoreMetadata


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
async def test_peek_returns_cached_snapshot_without_async_or_adapter_access() -> None:
    store = StateStore()
    snapshot = _snapshot(50)

    await store.save(snapshot)

    assert store.peek("light.kitchen", "brightness") == snapshot
    assert store.peek("light.kitchen", "missing") is None


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
async def test_equivalent_startup_reconfirmation_preserves_restored_metadata() -> None:
    store = StateStore()
    store.restore_metadata(
        StateStoreMetadata(
            inventory_revision=7,
            version_counter=12,
            state_versions={("light.kitchen", "brightness"): 12},
        )
    )
    snapshot = _snapshot(50)

    store.load_persisted([snapshot])
    assert store.runtime_revision == "rev-7"
    assert store.state_version("light.kitchen", "brightness") == 12
    restored = await store.get("light.kitchen", "brightness")
    assert restored is not None
    assert restored.status is StateStatus.STALE

    await store.save(snapshot)

    assert store.state_version("light.kitchen", "brightness") == 12


@pytest.mark.asyncio
async def test_changed_startup_reconfirmation_advances_restored_metadata() -> None:
    store = StateStore()
    store.restore_metadata(
        StateStoreMetadata(
            inventory_revision=7,
            version_counter=12,
            state_versions={("light.kitchen", "brightness"): 12},
        )
    )
    store.load_persisted([_snapshot(50)])

    await store.save(_snapshot(75))

    assert store.state_version("light.kitchen", "brightness") == 13


@pytest.mark.asyncio
async def test_changed_status_advances_version_even_with_same_value() -> None:
    store = StateStore()
    await store.save(_snapshot(50, status=StateStatus.CURRENT))
    version_after_first_save = store.state_version("light.kitchen", "brightness")

    await store.save(_snapshot(50, status=StateStatus.STALE))

    assert store.state_version("light.kitchen", "brightness") > version_after_first_save


@pytest.mark.asyncio
async def test_mark_stale_uses_injected_clock_when_no_explicit_now_given() -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    store = StateStore(timedelta(minutes=5), clock=clock)
    snapshot = StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=50,
        observed_at=initial,
        received_at=initial,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
    )
    await store.save(snapshot)

    stale = await store.mark_stale()
    assert stale == []

    clock.set(initial + timedelta(minutes=10))
    stale = await store.mark_stale()

    assert len(stale) == 1
    assert stale[0].status is StateStatus.STALE


@pytest.mark.asyncio
async def test_mark_stale_advances_version_for_transitioned_snapshot() -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    store = StateStore(timedelta(minutes=5), clock=clock)
    snapshot = StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=50,
        observed_at=initial,
        received_at=initial,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
    )
    await store.save(snapshot)
    version_before = store.state_version("light.kitchen", "brightness")

    clock.set(initial + timedelta(minutes=10))
    stale = await store.mark_stale()

    assert len(stale) == 1
    assert store.state_version("light.kitchen", "brightness") != version_before


@pytest.mark.asyncio
async def test_mark_all_stale_advances_version_for_transitioned_snapshot() -> None:
    store = StateStore()
    await store.save(_snapshot(50))
    version_before = store.state_version("light.kitchen", "brightness")

    stale = await store.mark_all_stale()

    assert len(stale) == 1
    assert store.state_version("light.kitchen", "brightness") != version_before


@pytest.mark.asyncio
async def test_mark_stale_does_not_advance_version_for_already_stale_snapshot() -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    store = StateStore(timedelta(minutes=5), clock=clock)
    snapshot = StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=50,
        observed_at=initial,
        received_at=initial,
        status=StateStatus.STALE,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
    )
    await store.save(snapshot)
    version_before = store.state_version("light.kitchen", "brightness")

    clock.set(initial + timedelta(minutes=10))
    stale = await store.mark_stale()

    assert stale == []
    assert store.state_version("light.kitchen", "brightness") == version_before


@pytest.mark.asyncio
async def test_mark_all_stale_does_not_advance_version_for_already_stale_snapshot() -> None:
    store = StateStore()
    await store.save(_snapshot(50, status=StateStatus.STALE))
    version_before = store.state_version("light.kitchen", "brightness")

    stale = await store.mark_all_stale()

    assert stale == []
    assert store.state_version("light.kitchen", "brightness") == version_before


@pytest.mark.asyncio
async def test_mark_stale_does_not_advance_version_for_snapshot_not_yet_stale() -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    store = StateStore(timedelta(minutes=5), clock=clock)
    snapshot = StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=50,
        observed_at=initial,
        received_at=initial,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
    )
    await store.save(snapshot)
    version_before = store.state_version("light.kitchen", "brightness")

    clock.set(initial + timedelta(minutes=1))
    stale = await store.mark_stale()

    assert stale == []
    assert store.state_version("light.kitchen", "brightness") == version_before


@pytest.mark.asyncio
async def test_effective_freshness_uses_receipt_age_and_does_not_mutate_store() -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial + timedelta(minutes=4))
    store = StateStore(timedelta(minutes=5), clock=clock)
    snapshot = StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=50,
        observed_at=initial,
        received_at=clock.now(),
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
    )
    await store.save(snapshot)

    effective = store.effective_snapshot(snapshot)

    assert effective.status is StateStatus.CURRENT
    assert (await store.get("light.kitchen", "brightness")).status is StateStatus.CURRENT

    clock.set(initial + timedelta(minutes=10))
    effective = store.effective_snapshot(snapshot)

    assert effective.status is StateStatus.STALE
    assert (await store.get("light.kitchen", "brightness")).status is StateStatus.CURRENT
