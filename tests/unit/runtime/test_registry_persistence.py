from __future__ import annotations

from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.domain.models import (
    AdapterSnapshot,
    AvailabilityStatus,
    Capability,
    CapabilityKind,
    Device,
    DeviceType,
    SourceRef,
)
from domoai.persistence.repositories import DeviceRepository
from domoai.persistence.sqlite import SQLiteDatabase
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


def test_live_inventory_removes_persisted_sources_from_unconfigured_adapters() -> None:
    registry = DeviceRegistry()
    persisted = _device()
    registry.load_persisted([persisted])

    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "light.live",
                    "name": "Live light",
                    "semantic_type": "light",
                    "capabilities": [_power_capability().model_dump(mode="json")],
                }
            ]
        ),
        "home_assistant",
        configured_adapter_ids={"home_assistant"},
    )

    assert registry.get(persisted.id) is None
    assert registry.canonical_id_for_source("fixture", persisted.id) is None


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
    assert (
        registry.resolve_command_route("living_room.main_light", "turn_on").reason
        == "route_not_found"
    )

    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")

    resolution = registry.resolve_command_route("living_room.main_light", "turn_on")
    assert resolution.route is not None


def test_rehydrated_identity_does_not_merge_replacement_with_same_local_name() -> None:
    registry = DeviceRegistry()
    registry.load_persisted(
        [
            Device(
                id="unassigned.lamp",
                type=DeviceType.LIGHT,
                name="Lamp",
                protocol="fixture",
                capabilities=[_power_capability()],
                source_refs=[SourceRef(adapter_id="fixture", external_id="old-lamp")],
            )
        ]
    )

    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "new-lamp",
                    "device_id": "new-device",
                    "name": "Lamp",
                    "semantic_type": "light",
                    "capabilities": [_power_capability().model_dump(mode="json")],
                }
            ]
        ),
        "fixture",
    )

    replacement_id = registry.canonical_id_for_source("fixture", "new-lamp")
    assert replacement_id == "unassigned.lamp-2"
    assert registry.get("unassigned.lamp") is None
    assert len(registry.devices) == 1


def test_rehydrated_identity_claims_preserve_canonical_id_after_entity_rename() -> None:
    registry = DeviceRegistry()
    registry.load_persisted(
        [
            Device(
                id="battery.home",
                type=DeviceType.ENERGY,
                name="Home battery",
                protocol="home_assistant",
                availability=AvailabilityStatus.AVAILABLE,
                capabilities=[_power_capability()],
                source_refs=[
                    SourceRef(adapter_id="home_assistant", external_id="sensor.old_power")
                ],
                identity_keys=["ha-device:stable-battery"],
                connections=["mac:aa:bb:cc:dd:ee:ff"],
            )
        ]
    )

    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "sensor.renamed_power",
                    "device_id": "ha-device-renamed",
                    "name": "Renamed battery",
                    "semantic_type": "energy",
                    "identity_keys": ["ha-device:stable-battery"],
                    "connections": ["mac:aa:bb:cc:dd:ee:ff"],
                    "capabilities": [_power_capability().model_dump(mode="json")],
                }
            ]
        ),
        "home_assistant",
    )

    assert registry.canonical_id_for_source("home_assistant", "sensor.renamed_power") == (
        "battery.home"
    )


@pytest.mark.asyncio
async def test_persisted_source_device_identity_rebinds_renamed_entity_after_restart(
    tmp_path: Path,
) -> None:
    first_run = DeviceRegistry()
    first_run.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "sensor.battery_power",
                    "device_id": "ha-device-stable-battery",
                    "name": "Battery power",
                    "area_id": "garage",
                    "semantic_type": "energy",
                    "capabilities": [_power_capability().model_dump(mode="json")],
                }
            ]
        ),
        "home_assistant",
    )
    original = first_run.devices[0]

    database = SQLiteDatabase(tmp_path / "identity.sqlite3")
    await database.initialize()
    repository = DeviceRepository(database)
    await repository.save(original)
    persisted = await repository.list_all()
    assert persisted[0].source_refs[0].source_device_id == "ha-device-stable-battery"

    restarted = DeviceRegistry()
    restarted.load_persisted(persisted)
    assert (
        restarted.resolve_command_route(original.id, "turn_on").reason
        == "route_not_found"
    )
    restarted.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "sensor.battery_power_renamed",
                    "device_id": "ha-device-stable-battery",
                    "name": "Renamed battery power",
                    "area_id": "garage",
                    "semantic_type": "energy",
                    "capabilities": [_power_capability().model_dump(mode="json")],
                }
            ]
        ),
        "home_assistant",
    )

    assert (
        restarted.canonical_id_for_source(
            "home_assistant", "sensor.battery_power_renamed"
        )
        == original.id
    )
    assert restarted.get(original.id) is not None
    assert restarted.resolve_command_route(original.id, "turn_on").route is not None
    assert restarted.canonical_id_for_source("home_assistant", "sensor.battery_power") is None
    await database.close()


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
