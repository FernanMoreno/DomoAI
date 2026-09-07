from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.event_consumer import RuntimeEventConsumer
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import (
    Command,
    Plan,
    SourceCursor,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
    StateStatus,
)
from domoai.persistence.repositories import (
    DeviceRepository,
    PlanRepository,
    RuntimeStateMetadataRepository,
    RuntimeStatePersistenceRepository,
    StateSnapshotRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


class _CountingPersistence:
    def __init__(self) -> None:
        self.persist_calls = 0

    async def persist(self, snapshots, metadata) -> None:
        del snapshots, metadata
        self.persist_calls += 1

    async def delete(self, device_id, metadata) -> None:
        del device_id, metadata


class _CountingLegacySink:
    def __init__(self) -> None:
        self.save_calls = 0

    async def save(self, snapshot) -> None:
        del snapshot
        self.save_calls += 1


class _FailingRuntimePersistence(RuntimeStatePersistenceRepository):
    def __init__(self, database, *, clock=None) -> None:
        super().__init__(database, clock=clock)
        self.fail = False

    async def persist(self, snapshots, metadata) -> None:
        if self.fail:
            raise RuntimeError("sqlite persistence failed")
        await super().persist(snapshots, metadata)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_discovery_event_and_executor_readback_restore_one_state_revision(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    database = SQLiteDatabase(tmp_path / "state-coherence.sqlite3", clock=clock)
    await database.initialize()
    snapshot_repository = StateSnapshotRepository(database)
    metadata_repository = RuntimeStateMetadataRepository(database, clock=clock)
    persistence = RuntimeStatePersistenceRepository(database, clock=clock)
    state_store = StateStore(clock=clock)
    state_store.bind_persistence(persistence)
    adapter = SimulatedHomeAdapter(clock=clock)
    registry = DeviceRegistry()
    audit = AuditLog(clock=clock)
    discovery = DiscoveryService(
        adapter,
        registry,
        state_store,
        audit,
        device_repository=DeviceRepository(database, clock=clock),
        state_snapshot_repository=snapshot_repository,
        runtime_state_metadata_repository=metadata_repository,
        clock=clock,
    )
    await discovery.refresh()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    source_entity = "light.living_room_main"
    initial_version = state_store.state_version(light_id, "power")

    adapter._find(source_entity)["state"]["power"] = True
    adapter._events.append(
        StateChangedEvent(
            source_adapter_id=adapter.adapter_id,
            external_id=source_entity,
            capability="power",
        )
    )
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)
    await consumer.consume_once()
    event_version = state_store.state_version(light_id, "power")
    assert event_version > initial_version

    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    plan = plan_service.validate(
        Plan(
            id="state-coherence-readback",
            commands=[
                Command(
                    id="state-coherence-command",
                    device_id=light_id,
                    command="turn_off",
                    idempotency_key="state-coherence-command-key",
                )
            ],
        )
    )
    plan_repository = PlanRepository(database, clock=clock)
    await plan_repository.save_validation(plan)
    summary = await PlanExecutor(
        adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        clock=clock,
    ).execute(plan)
    assert summary.outcomes[0].status.value == "confirmed_success"
    final_version = state_store.state_version(light_id, "power")
    assert final_version > event_version

    persisted_metadata = await metadata_repository.get()
    assert persisted_metadata is not None
    assert persisted_metadata.state_versions[(light_id, "power")] == final_version
    persisted_snapshot = await snapshot_repository.list_all()
    final_snapshot = next(
        item for item in persisted_snapshot
        if item.device_id == light_id and item.capability == "power"
    )
    assert final_snapshot.value is False

    restarted = StateStore(clock=clock)
    restarted.restore_metadata(persisted_metadata)
    restarted.load_persisted(persisted_snapshot)
    assert restarted.state_version(light_id, "power") == final_version
    restored = restarted.peek(light_id, "power")
    assert restored is not None
    assert restored.value is False
    assert restored.status.value == "stale"


@pytest.mark.composition
@pytest.mark.asyncio
async def test_executor_uses_one_authoritative_readback_persistence_path() -> None:
    persistence = _CountingPersistence()
    legacy_sink = _CountingLegacySink()
    state_store = StateStore()
    state_store.bind_persistence(persistence)
    plan_service = PlanService(DeviceRegistry(), state_store, PolicyEngine([]), AuditLog())
    executor = PlanExecutor(
        SimulatedHomeAdapter(),
        plan_service,
        AuditLog(),
        state_snapshot_repository=legacy_sink,
    )
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    snapshot = StateSnapshot(
        device_id="light.main",
        capability="power",
        value=False,
        observed_at=now,
        received_at=now,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.main"),
    )

    await executor._persist_snapshot(snapshot)

    assert persistence.persist_calls == 1
    assert legacy_sink.save_calls == 0


@pytest.mark.composition
@pytest.mark.asyncio
async def test_sqlite_restart_restores_last_committed_state_after_failed_replacement(
    tmp_path,
) -> None:
    database_path = tmp_path / "state-integrity.sqlite3"
    clock = FixedClock(datetime(2026, 9, 3, 12, tzinfo=UTC))
    database = SQLiteDatabase(database_path, clock=clock)
    await database.initialize()
    persistence = _FailingRuntimePersistence(database, clock=clock)
    state_store = StateStore(clock=clock)
    state_store.bind_persistence(persistence)
    initial = StateSnapshot(
        device_id="light.main",
        capability="power",
        value=False,
        observed_at=clock.now(),
        received_at=clock.now(),
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light.main"),
        source_cursor=SourceCursor(
            source_id="fixture", stream_id="events", epoch="boot-1", sequence=1
        ),
    )
    await state_store.save(initial)
    persistence.fail = True

    replacement = initial.model_copy(
        update={
            "value": True,
            "source_cursor": SourceCursor(
                source_id="fixture", stream_id="events", epoch="boot-1", sequence=2
            ),
        }
    )
    with pytest.raises(RuntimeError, match="sqlite persistence failed"):
        await state_store.save(replacement)

    live = state_store.peek("light.main", "power")
    assert live is not None
    assert live.value is False
    assert state_store.export_metadata().source_cursors[("fixture", "events")].sequence == 1
    assert state_store.durability_status == "degraded"
    await database.close()

    restarted_database = SQLiteDatabase(database_path, clock=clock)
    await restarted_database.initialize()
    persisted_metadata = await RuntimeStateMetadataRepository(restarted_database).get()
    persisted_snapshots = await StateSnapshotRepository(restarted_database).list_all()
    assert persisted_metadata is not None
    restarted = StateStore(clock=clock)
    restarted.restore_metadata(persisted_metadata)
    restarted.load_persisted(persisted_snapshots)

    restored = restarted.peek("light.main", "power")
    assert restored is not None
    assert restored.value is False
    assert restored.status is StateStatus.STALE
    assert restarted.export_metadata().source_cursors[("fixture", "events")].sequence == 1
    await restarted_database.close()
