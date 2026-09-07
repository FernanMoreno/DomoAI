import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.models import SourceCursor, SourceRef, StateSnapshot, StateStatus
from domoai.runtime.clock import FixedClock
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics
from domoai.runtime.state_store import StateStore, StateStoreMetadata


class _TogglePersistence:
    def __init__(self) -> None:
        self.fail = False

    async def persist(self, snapshots, metadata) -> None:
        del snapshots, metadata
        if self.fail:
            raise RuntimeError("persistence failed")

    async def delete(self, device_id, metadata) -> None:
        del device_id, metadata


class _OutOfOrderPersistence:
    def __init__(self) -> None:
        self.old_started = asyncio.Event()
        self.release_old = asyncio.Event()

    async def persist(self, snapshots, metadata) -> None:
        del metadata
        if snapshots and snapshots[0].value is True:
            self.old_started.set()
            await self.release_old.wait()

    async def delete(self, device_id, metadata) -> None:
        del device_id, metadata


class _CancellationPersistence:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.committed = False

    async def persist(self, snapshots, metadata) -> None:
        del snapshots, metadata
        self.started.set()
        await self.release.wait()
        self.committed = True

    async def delete(self, device_id, metadata) -> None:
        del device_id, metadata


def _snapshot(
    value: object,
    *,
    status: StateStatus = StateStatus.CURRENT,
    cursor: SourceCursor | None = None,
) -> StateSnapshot:
    return StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=value,
        observed_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        status=status,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
        source_cursor=cursor,
    )


def _snapshot_at(value: object, received_at: datetime) -> StateSnapshot:
    return StateSnapshot(
        device_id="light.kitchen",
        capability="brightness",
        value=value,
        observed_at=received_at,
        received_at=received_at,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
    )


def _cursor(sequence: int, *, epoch: str = "boot-1", resync: bool = False) -> SourceCursor:
    return SourceCursor(
        source_id="fixture",
        stream_id="events",
        epoch=epoch,
        sequence=sequence,
        resync=resync,
    )


@pytest.mark.asyncio
async def test_state_version_starts_at_zero_for_unknown_key() -> None:
    store = StateStore()

    assert store.state_version("light.kitchen", "brightness") == 0


@pytest.mark.asyncio
async def test_forget_household_evicts_legacy_deployment_scoped_cache() -> None:
    store = StateStore()
    await store.save(_snapshot(50))

    assert await store.forget_household("home-a") == 1
    assert store.peek("light.kitchen", "brightness") is None


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


@pytest.mark.asyncio
async def test_ordered_source_accepts_first_cursor_and_restores_ordering_metadata() -> None:
    store = StateStore()

    await store.save(_snapshot(50, cursor=_cursor(22)))

    metadata = store.export_metadata()
    assert metadata.source_cursors[("fixture", "events")] == _cursor(22)
    assert metadata.source_ordering_policies[("fixture", "events")] == "ordered"
    assert metadata.resync_required == {}


@pytest.mark.asyncio
async def test_old_cursor_is_ignored_without_replacing_state_or_version() -> None:
    store = StateStore()
    await store.save(_snapshot(50, cursor=_cursor(22)))
    version = store.state_version("light.kitchen", "brightness")

    await store.save(_snapshot(18, cursor=_cursor(18)))

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.value == 50
    assert store.state_version("light.kitchen", "brightness") == version
    assert store.export_metadata().source_cursors[("fixture", "events")].sequence == 22


@pytest.mark.asyncio
async def test_duplicate_cursor_is_ignored_even_when_payload_value_differs() -> None:
    store = StateStore()
    await store.save(_snapshot(50, cursor=_cursor(22)))
    version = store.state_version("light.kitchen", "brightness")

    await store.save(_snapshot(99, cursor=_cursor(22)))

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.value == 50
    assert store.state_version("light.kitchen", "brightness") == version


@pytest.mark.asyncio
async def test_gap_marks_canonical_state_invalid_and_requires_resync() -> None:
    store = StateStore()
    await store.save(_snapshot(50, cursor=_cursor(1)))

    await store.save(_snapshot(75, cursor=_cursor(3)))

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.status is StateStatus.INVALID
    assert snapshot.value is None
    assert store.export_metadata().resync_required[("fixture", "events")] == "sequence_gap"
    assert store.export_metadata().source_cursors[("fixture", "events")].sequence == 1


@pytest.mark.asyncio
async def test_epoch_change_requires_resync_and_does_not_advance_cursor() -> None:
    store = StateStore()
    await store.save(_snapshot(50, cursor=_cursor(4)))

    await store.save(_snapshot(75, cursor=_cursor(1, epoch="boot-2")))

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.status is StateStatus.INVALID
    assert store.export_metadata().resync_required[("fixture", "events")] == "epoch_changed"
    assert store.export_metadata().source_cursors[("fixture", "events")].epoch == "boot-1"


@pytest.mark.asyncio
async def test_explicit_resync_accepts_new_epoch_and_clears_degradation() -> None:
    store = StateStore()
    await store.save(_snapshot(50, cursor=_cursor(4)))
    await store.save(_snapshot(75, cursor=_cursor(1, epoch="boot-2")))

    await store.save(_snapshot(75, cursor=_cursor(1, epoch="boot-2", resync=True)))

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.status is StateStatus.CURRENT
    assert snapshot.value == 75
    metadata = store.export_metadata()
    assert metadata.source_cursors[("fixture", "events")].epoch == "boot-2"
    assert metadata.resync_required == {}


@pytest.mark.asyncio
async def test_cursor_decisions_are_projected_to_operational_metrics() -> None:
    metrics = RuntimeOperationalMetrics()
    store = StateStore(operational_metrics=metrics)

    await store.save(_snapshot(10, cursor=_cursor(1)))
    await store.save(_snapshot(20, cursor=_cursor(3)))
    await store.save(_snapshot(30, cursor=_cursor(1)))
    await store.save(_snapshot(40, cursor=_cursor(1, epoch="boot-2", resync=True)))

    assert metrics.snapshot()["source_cursor"] == {
        "gap_total": 1,
        "replay_total": 1,
        "resync_total": 1,
    }


@pytest.mark.asyncio
async def test_cursorless_source_is_explicitly_unordered() -> None:
    store = StateStore()

    await store.save(_snapshot(50))

    metadata = store.export_metadata()
    assert metadata.source_ordering_policies[("fixture", "unordered")] == "unordered"
    assert metadata.source_cursors == {}


@pytest.mark.asyncio
async def test_cursorless_source_ignores_an_observation_with_an_older_receipt() -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    store = StateStore()

    await store.save(_snapshot_at(False, initial + timedelta(seconds=2)))
    version = store.state_version("light.kitchen", "brightness")

    await store.save(_snapshot_at(True, initial + timedelta(seconds=1)))

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.value is False
    assert store.state_version("light.kitchen", "brightness") == version


@pytest.mark.asyncio
async def test_cursorless_old_degradation_does_not_replace_current_state() -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    store = StateStore()

    await store.save(_snapshot_at(False, initial + timedelta(seconds=2)))
    await store.save(
        StateSnapshot(
            device_id="light.kitchen",
            capability="brightness",
            value=None,
            observed_at=initial + timedelta(seconds=1),
            received_at=initial + timedelta(seconds=1),
            status=StateStatus.UNAVAILABLE,
            source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
        )
    )

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.value is False
    assert snapshot.status is StateStatus.CURRENT


@pytest.mark.asyncio
async def test_state_writes_are_serialized_before_out_of_order_persistence_can_commit() -> None:
    persistence = _OutOfOrderPersistence()
    store = StateStore()
    store.bind_persistence(persistence)
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)

    old_write = asyncio.create_task(store.save(_snapshot_at(True, initial + timedelta(seconds=1))))
    await asyncio.wait_for(persistence.old_started.wait(), timeout=1)
    new_write = asyncio.create_task(store.save(_snapshot_at(False, initial + timedelta(seconds=2))))

    persistence.release_old.set()
    await asyncio.gather(old_write, new_write)

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.value is False


@pytest.mark.asyncio
async def test_failed_persistence_keeps_previous_view_and_metadata_authoritative() -> None:
    persistence = _TogglePersistence()
    store = StateStore()
    store.bind_persistence(persistence)
    await store.save(_snapshot(50, cursor=_cursor(1)))
    previous_metadata = store.export_metadata()
    previous_version = store.state_version("light.kitchen", "brightness")
    persistence.fail = True

    with pytest.raises(RuntimeError, match="persistence failed"):
        await store.save(_snapshot(75, cursor=_cursor(2)))

    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.value == 50
    assert store.state_version("light.kitchen", "brightness") == previous_version
    assert store.export_metadata() == previous_metadata
    assert store.durability_status == "degraded"


@pytest.mark.asyncio
async def test_cancelled_persistence_installs_candidate_after_durable_commit() -> None:
    persistence = _CancellationPersistence()
    store = StateStore()
    store.bind_persistence(persistence)
    save_task = asyncio.create_task(store.save(_snapshot(75)))

    await asyncio.wait_for(persistence.started.wait(), timeout=1)
    save_task.cancel()
    persistence.release.set()

    with pytest.raises(asyncio.CancelledError):
        await save_task

    assert persistence.committed is True
    snapshot = store.peek("light.kitchen", "brightness")
    assert snapshot is not None
    assert snapshot.value == 75
    assert store.durability_status == "committed"


@pytest.mark.asyncio
async def test_failed_metadata_persistence_rolls_back_revision_and_fingerprint() -> None:
    persistence = _TogglePersistence()
    store = StateStore()
    store.bind_persistence(persistence)
    store.begin_revision()
    store.record_inventory_fingerprint("new-fingerprint")
    persistence.fail = True

    with pytest.raises(RuntimeError, match="persistence failed"):
        await store.persist_metadata()

    assert store.runtime_revision == "rev-0"
    assert store.inventory_fingerprint is None
    assert store.durability_status == "degraded"
