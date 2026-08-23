from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import AdapterSnapshot
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.multi_adapter import RecordingAdapter, entity, power_capability


@pytest.mark.asyncio
async def test_discovery_preserves_aware_source_observation_timestamp() -> None:
    observed_at = datetime(2026, 1, 1, 11, 59, tzinfo=UTC)
    adapter = RecordingAdapter(
        "fixture",
        AdapterSnapshot(
            source_entities=[
                entity(
                    entity_id="light.timestamped",
                    source_device_id="timestamped-light",
                    canonical_id="timestamped.light",
                    name="Timestamped light",
                    capabilities=[power_capability()],
                )
            ],
            source_states=[
                {
                    "entity_id": "light.timestamped",
                    "capability": "power",
                    "value": True,
                    "unit": None,
                    "available": True,
                    "observed_at": observed_at,
                    "received_at": observed_at,
                }
            ],
        ),
    )
    fixed = FixedClock(datetime(2026, 1, 1, 12, tzinfo=UTC))
    state_store = StateStore()
    service = DiscoveryService(adapter, DeviceRegistry(), state_store, AuditLog(), clock=fixed)

    await service.refresh()

    snapshot = await state_store.get("timestamped.light", "power")
    assert snapshot is not None
    assert snapshot.observed_at == observed_at
    assert snapshot.received_at == fixed.now()


@pytest.mark.asyncio
async def test_discovery_builds_semantic_inventory_and_states() -> None:
    adapter = SimulatedHomeAdapter()
    service = DiscoveryService(adapter, DeviceRegistry(), StateStore(), AuditLog())

    result = await service.refresh()

    assert {device.type.value for device in result.devices} == {
        "light",
        "switch",
        "cover",
        "climate",
        "sensor",
        "energy",
    }
    assert {area.id for area in result.areas} == {"living_room", "garden", "bedroom", "garage"}
    assert any(state.capability == "brightness" for state in result.states)
    assert result.runtime_revision == "rev-1"


@pytest.mark.asyncio
async def test_discovery_preserves_canonical_id_after_source_rename() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    service = DiscoveryService(adapter, registry, StateStore(), AuditLog())

    first = await service.refresh()
    first_id = next(device.id for device in first.devices if device.type.value == "light")

    adapter.rename("light.living_room_main", "Renamed living room lamp")
    second = await service.refresh()
    second_id = next(device.id for device in second.devices if device.type.value == "light")

    assert second_id == first_id


@pytest.mark.asyncio
async def test_unavailable_source_is_explicit_in_semantic_state() -> None:
    adapter = SimulatedHomeAdapter()
    adapter.set_available("cover.bedroom_blind", False)
    state_store = StateStore()
    service = DiscoveryService(adapter, DeviceRegistry(), state_store, AuditLog())

    result = await service.refresh()
    cover = next(device for device in result.devices if device.type.value == "cover")
    position = await state_store.get(cover.id, "position")

    assert position is not None
    assert position.status.value == "unavailable"


@pytest.mark.asyncio
async def test_repeat_refresh_with_no_inventory_change_does_not_advance_revision() -> None:
    adapter = SimulatedHomeAdapter()
    state_store = StateStore()
    service = DiscoveryService(adapter, DeviceRegistry(), state_store, AuditLog())

    first = await service.refresh()
    second = await service.refresh()

    assert first.runtime_revision == "rev-1"
    assert second.runtime_revision == "rev-1"


@pytest.mark.asyncio
async def test_availability_change_still_advances_revision() -> None:
    adapter = SimulatedHomeAdapter()
    state_store = StateStore()
    service = DiscoveryService(adapter, DeviceRegistry(), state_store, AuditLog())

    await service.refresh()
    adapter.set_available("cover.bedroom_blind", False)
    second = await service.refresh()

    assert second.runtime_revision == "rev-2"


@pytest.mark.asyncio
async def test_same_source_capability_limit_change_advances_revision() -> None:
    adapter = SimulatedHomeAdapter()
    state_store = StateStore()
    registry = DeviceRegistry()
    service = DiscoveryService(adapter, registry, state_store, AuditLog())

    first = await service.refresh()
    assert first.runtime_revision == "rev-1"
    next(entity for entity in adapter._entities if entity["entity_id"] == "light.living_room_main")[
        "attributes"
    ]["brightness_max"] = 50

    second = await service.refresh()

    assert second.runtime_revision == "rev-2"
    light = next(device for device in second.devices if device.type.value == "light")
    brightness = next(
        capability for capability in light.capabilities if capability.name == "brightness"
    )
    assert brightness.maximum == 50
