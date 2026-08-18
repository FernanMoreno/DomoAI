from __future__ import annotations

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
