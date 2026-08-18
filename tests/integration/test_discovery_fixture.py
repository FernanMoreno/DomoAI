import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


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
