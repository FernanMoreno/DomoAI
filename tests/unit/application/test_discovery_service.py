from __future__ import annotations

import pytest

from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import AdapterSnapshot, StateStatus
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.multi_adapter import RecordingAdapter, entity, power_capability


def _shared_device_snapshot(
    *, entity_id: str, source_device_id: str, value: bool
) -> AdapterSnapshot:
    shared_entity = entity(
        entity_id=entity_id,
        source_device_id=source_device_id,
        canonical_id="shared.state_conflict_device",
        name="Shared Power",
        capabilities=[power_capability()],
    )
    return AdapterSnapshot(
        source_entities=[shared_entity],
        source_states=[
            {
                "entity_id": entity_id,
                "capability": "power",
                "value": value,
                "unit": None,
                "available": True,
            }
        ],
    )


@pytest.mark.asyncio
async def test_disagreeing_cross_adapter_state_is_audited_as_state_source_conflict() -> None:
    first = RecordingAdapter(
        "home_assistant",
        _shared_device_snapshot(
            entity_id="light.power_a", source_device_id="physical-shared-1", value=True
        ),
    )
    second = RecordingAdapter(
        "modbus",
        _shared_device_snapshot(
            entity_id="modbus.power_b", source_device_id="modbus-physical-shared-1", value=False
        ),
    )
    registry = DeviceRegistry()
    composite = CompositeAdapter([first, second], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    await discovery.refresh()

    conflict_events = [
        event for event in audit.events if event.event_type == "state_source_conflict"
    ]
    assert len(conflict_events) == 1
    payload = conflict_events[0].payload
    assert payload["capability"] == "power"
    assert conflict_events[0].subject_id == "shared.state_conflict_device"
    reported_adapters = {source["adapter_id"] for source in payload["sources"]}
    assert reported_adapters == {"home_assistant", "modbus"}

    cached = await state_store.get("shared.state_conflict_device", "power")
    assert cached is not None
    assert cached.status is StateStatus.INVALID
    assert cached.value is None


@pytest.mark.asyncio
async def test_agreeing_cross_adapter_state_records_no_conflict() -> None:
    first = RecordingAdapter(
        "home_assistant",
        _shared_device_snapshot(
            entity_id="light.power_a", source_device_id="physical-shared-1", value=True
        ),
    )
    second = RecordingAdapter(
        "modbus",
        _shared_device_snapshot(
            entity_id="modbus.power_b", source_device_id="modbus-physical-shared-1", value=True
        ),
    )
    registry = DeviceRegistry()
    composite = CompositeAdapter([first, second], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    await discovery.refresh()

    assert not any(event.event_type == "state_source_conflict" for event in audit.events)
    cached = await state_store.get("shared.state_conflict_device", "power")
    assert cached is not None
    assert cached.status is StateStatus.CURRENT
    assert cached.value is True


@pytest.mark.asyncio
async def test_single_source_capability_records_no_conflict() -> None:
    adapter = RecordingAdapter(
        "home_assistant",
        _shared_device_snapshot(
            entity_id="light.power_a", source_device_id="physical-shared-1", value=True
        ),
    )
    registry = DeviceRegistry()
    composite = CompositeAdapter([adapter], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    await discovery.refresh()

    assert not any(event.event_type == "state_source_conflict" for event in audit.events)
    cached = await state_store.get("shared.state_conflict_device", "power")
    assert cached is not None
    assert cached.status is StateStatus.CURRENT
    assert cached.value is True


@pytest.mark.asyncio
async def test_refresh_stamps_state_snapshots_from_the_injected_clock() -> None:
    from datetime import UTC, datetime

    from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
    from domoai.runtime.clock import FixedClock

    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    fixed = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    discovery = DiscoveryService(adapter, registry, state_store, audit, clock=fixed)

    await discovery.refresh()

    snapshots = await state_store.all()
    assert snapshots
    assert all(snapshot.received_at == fixed.now() for snapshot in snapshots)
