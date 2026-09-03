from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from domoai.application.discovery_service import DiscoveryService
from domoai.application.state_refresher import RuntimeStateRefresher
from domoai.domain.models import (
    AdapterHealth,
    AdapterSnapshot,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


class _Adapter:
    adapter_id = "fixture"

    def __init__(self, clock: FixedClock, *, cached: bool = False) -> None:
        self.clock = clock
        self.cached = cached
        self.reads = 0

    async def discover(self) -> AdapterSnapshot:
        timestamp = self.clock.now()
        return AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "sensor.temperature",
                    "device_id": "fixture-device",
                    "domain": "sensor",
                    "name": "Temperature",
                    "semantic_type": "sensor",
                    "protocol": "fixture",
                    "capabilities": [
                        {
                            "name": "temperature",
                            "kind": "number",
                            "unit": "°C",
                            "readable": True,
                            "writable": False,
                        }
                    ],
                }
            ],
            source_states=[
                {
                    "entity_id": "sensor.temperature",
                    "capability": "temperature",
                    "value": 20,
                    "unit": "°C",
                    "observed_at": timestamp,
                    "received_at": timestamp,
                    "available": True,
                }
            ],
        )

    async def read_state(self, source_refs: list[SourceRef]) -> list[StateSnapshot]:
        self.reads += 1
        timestamp = datetime(2026, 8, 19, 12, tzinfo=UTC) if self.cached else self.clock.now()
        return [
            StateSnapshot(
                device_id="fixture-device",
                capability="temperature",
                value=20,
                unit="°C",
                observed_at=timestamp,
                received_at=timestamp,
                status=StateStatus.CURRENT,
                source_ref=source_refs[0],
            )
        ]


class _PartialHealthAdapter:
    adapter_id = "composite"

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=True,
            components=[
                AdapterHealth(adapter_id="home_assistant", connected=True),
                AdapterHealth(adapter_id="matter", connected=False),
            ],
        )


class _NoopDiscovery:
    def __init__(self) -> None:
        self.refreshes = 0
        self.state_refreshes = 0

    async def refresh(self) -> tuple[object, ...]:
        self.refreshes += 1
        return ()

    async def refresh_state(self) -> tuple[object, ...]:
        self.state_refreshes += 1
        return ()


class _FilteredDiscovery(_NoopDiscovery):
    def __init__(self) -> None:
        super().__init__()
        self.excluded_adapter_ids: frozenset[str] = frozenset()

    async def refresh_state(
        self, *, exclude_adapter_ids: frozenset[str] = frozenset()
    ) -> tuple[object, ...]:
        self.excluded_adapter_ids = exclude_adapter_ids
        self.state_refreshes += 1
        return ()

    async def refresh(
        self, *, exclude_adapter_ids: frozenset[str] = frozenset()
    ) -> tuple[object, ...]:
        self.refreshes += 1
        return SimpleNamespace(states=())  # type: ignore[return-value]


class _EventDrivenCompositeAdapter:
    adapter_id = "composite"
    event_driven_state_adapter_ids = frozenset({"knx"})

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=True)


class _ConfiguredChild:
    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id


class _StaticInventoryCompositeAdapter:
    adapter_id = "composite"
    static_inventory_adapter_ids = frozenset({"knx"})
    adapters = (_ConfiguredChild("knx"), _ConfiguredChild("mqtt"))

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=True)


@pytest.mark.asyncio
async def test_runtime_refresher_updates_stable_source_state() -> None:
    clock = FixedClock(datetime(2026, 8, 19, 12, tzinfo=UTC))
    adapter = _Adapter(clock)
    state_store = StateStore(timedelta(minutes=5), clock=clock)
    discovery = DiscoveryService(adapter, DeviceRegistry(), state_store, AuditLog(), clock=clock)
    result = await discovery.refresh()
    canonical_id = result.devices[0].id
    clock.set(clock.now() + timedelta(minutes=1))
    refresher = RuntimeStateRefresher(discovery, state_store, AuditLog(), interval_seconds=30)

    await refresher.refresh_once()

    state = await state_store.get(canonical_id, "temperature")
    assert state is not None
    assert state.received_at == clock.now()
    assert state.status is StateStatus.CURRENT
    assert adapter.reads == 1
    assert refresher.last_refresh_at == clock.now()


@pytest.mark.asyncio
async def test_runtime_refresher_does_not_rejuvenate_cached_source_state() -> None:
    clock = FixedClock(datetime(2026, 8, 19, 12, tzinfo=UTC))
    adapter = _Adapter(clock, cached=True)
    state_store = StateStore(timedelta(minutes=5), clock=clock)
    discovery = DiscoveryService(adapter, DeviceRegistry(), state_store, AuditLog(), clock=clock)
    result = await discovery.refresh()
    canonical_id = result.devices[0].id
    clock.set(clock.now() + timedelta(minutes=10))
    refresher = RuntimeStateRefresher(discovery, state_store, AuditLog(), interval_seconds=30)

    await refresher.refresh_once()

    state = await state_store.get(canonical_id, "temperature")
    assert state is not None
    assert state.received_at == datetime(2026, 8, 19, 12, tzinfo=UTC)
    assert state.status is StateStatus.CURRENT
    assert state_store.effective_snapshot(state).status is StateStatus.STALE


@pytest.mark.asyncio
async def test_runtime_refresher_marks_disconnected_child_source_unavailable() -> None:
    clock = FixedClock(datetime(2026, 8, 19, 12, tzinfo=UTC))
    state_store = StateStore(timedelta(minutes=5), clock=clock)
    await state_store.save(
        StateSnapshot(
            device_id="matter-light",
            capability="power",
            value=True,
            observed_at=clock.now(),
            received_at=clock.now(),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="matter", external_id="node:5/endpoint:1"),
        )
    )
    refresher = RuntimeStateRefresher(
        _NoopDiscovery(),
        state_store,
        AuditLog(),
        interval_seconds=30,
        adapter=_PartialHealthAdapter(),
        clock=clock,
    )

    await refresher.refresh_once()

    state = await state_store.get("matter-light", "power")
    assert state is not None
    assert state.status is StateStatus.UNAVAILABLE
    assert state.value is None


@pytest.mark.asyncio
async def test_runtime_refresher_reconciles_inventory_on_configured_cadence() -> None:
    clock = FixedClock(datetime(2026, 8, 19, 12, tzinfo=UTC))
    discovery = _NoopDiscovery()
    refresher = RuntimeStateRefresher(
        discovery,  # type: ignore[arg-type]
        StateStore(timedelta(minutes=5), clock=clock),
        AuditLog(),
        interval_seconds=30,
        inventory_refresh_interval_seconds=timedelta(minutes=1).total_seconds(),
        adapter=_PartialHealthAdapter(),
        clock=clock,
    )

    await refresher.refresh_once()
    clock.set(clock.now() + timedelta(seconds=59))
    await refresher.refresh_once()
    clock.set(clock.now() + timedelta(seconds=1))
    await refresher.refresh_once()

    assert discovery.state_refreshes == 2
    assert discovery.refreshes == 1


@pytest.mark.asyncio
async def test_runtime_refresher_excludes_event_driven_sources_from_polling() -> None:
    discovery = _FilteredDiscovery()
    refresher = RuntimeStateRefresher(
        discovery,  # type: ignore[arg-type]
        StateStore(timedelta(minutes=5)),
        AuditLog(),
        interval_seconds=30,
        adapter=_EventDrivenCompositeAdapter(),  # type: ignore[arg-type]
    )

    await refresher.refresh_once()

    assert discovery.excluded_adapter_ids == frozenset({"knx"})


@pytest.mark.asyncio
async def test_runtime_refresher_polls_static_sources_on_inventory_refresh() -> None:
    clock = FixedClock(datetime(2026, 8, 19, 12, tzinfo=UTC))
    discovery = _FilteredDiscovery()
    refresher = RuntimeStateRefresher(
        discovery,  # type: ignore[arg-type]
        StateStore(timedelta(minutes=5), clock=clock),
        AuditLog(),
        interval_seconds=30,
        inventory_refresh_interval_seconds=60,
        adapter=_StaticInventoryCompositeAdapter(),  # type: ignore[arg-type]
        clock=clock,
    )

    await refresher.refresh_once()
    clock.set(clock.now() + timedelta(seconds=60))
    await refresher.refresh_once()

    assert discovery.refreshes == 1
    assert discovery.state_refreshes == 2
    assert discovery.excluded_adapter_ids == frozenset({"mqtt"})
