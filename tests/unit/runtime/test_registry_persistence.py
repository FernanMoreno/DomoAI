from __future__ import annotations

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.domain.models import (
    AvailabilityStatus,
    Capability,
    CapabilityKind,
    Device,
    DeviceType,
    SourceRef,
)
from domoai.runtime.registry import DeviceRegistry


def _power_capability() -> Capability:
    return Capability(
        name="power",
        kind=CapabilityKind.BOOLEAN,
        readable=True,
        writable=True,
        commands=["turn_on", "turn_off"],
    )


def _device(device_id: str = "light.kitchen") -> Device:
    return Device(
        id=device_id,
        type=DeviceType.LIGHT,
        name=device_id,
        protocol="fixture",
        availability=AvailabilityStatus.AVAILABLE,
        capabilities=[_power_capability()],
        source_refs=[SourceRef(adapter_id="fixture", external_id=device_id)],
    )


def test_load_persisted_makes_device_readable() -> None:
    registry = DeviceRegistry()
    device = _device()

    registry.load_persisted([device])

    assert registry.get(device.id) == device
    assert registry.canonical_id_for_source("fixture", "light.kitchen") == device.id


def test_load_persisted_device_is_not_executable() -> None:
    registry = DeviceRegistry()
    registry.load_persisted([_device()])

    resolution = registry.resolve_command_route("light.kitchen", "turn_on")

    assert resolution.route is None
    assert resolution.reason == "route_not_found"


def test_live_rediscovery_after_load_persisted_makes_device_executable() -> None:
    from tests.fixtures.multi_adapter import source_snapshot

    registry = DeviceRegistry()
    registry.load_persisted(
        [
            Device(
                id="living_room.main_light",
                type=DeviceType.LIGHT,
                name="Main Light",
                protocol="home_assistant",
                availability=AvailabilityStatus.AVAILABLE,
                capabilities=[_power_capability()],
                source_refs=[
                    SourceRef(adapter_id="home_assistant", external_id="light.main_power")
                ],
            )
        ]
    )
    assert registry.resolve_command_route(
        "living_room.main_light", "turn_on"
    ).reason == "route_not_found"

    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")

    resolution = registry.resolve_command_route("living_room.main_light", "turn_on")
    assert resolution.route is not None


@pytest.mark.asyncio
async def test_restart_and_rediscover_does_not_duplicate_devices() -> None:
    adapter = SimulatedHomeAdapter()
    await adapter.connect()
    snapshot = await adapter.discover()

    first_run = DeviceRegistry()
    first_run.apply_snapshot(snapshot, adapter.adapter_id)
    original_devices = first_run.devices
    original_ids = {device.id for device in original_devices}

    restarted = DeviceRegistry()
    restarted.load_persisted(original_devices)
    restarted.apply_snapshot(snapshot, adapter.adapter_id)

    restarted_ids = {device.id for device in restarted.devices}
    assert restarted_ids == original_ids

    sample_device, sample_command = next(
        (device, command)
        for device in original_devices
        for capability in device.capabilities
        if capability.writable
        for command in capability.commands
    )
    resolution = restarted.resolve_command_route(sample_device.id, sample_command)
    assert resolution.route is not None


@pytest.mark.asyncio
async def test_repeated_restart_cycles_keep_device_count_stable() -> None:
    adapter = SimulatedHomeAdapter()
    await adapter.connect()
    snapshot = await adapter.discover()

    registry = DeviceRegistry()
    registry.apply_snapshot(snapshot, adapter.adapter_id)
    original_ids = {device.id for device in registry.devices}

    for _ in range(3):
        devices = registry.devices
        registry = DeviceRegistry()
        registry.load_persisted(devices)
        registry.apply_snapshot(snapshot, adapter.adapter_id)
        assert {device.id for device in registry.devices} == original_ids
