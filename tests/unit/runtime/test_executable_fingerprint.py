from datetime import UTC, datetime

from domoai.domain.models import (
    AvailabilityStatus,
    Capability,
    CapabilityKind,
    Device,
    DeviceType,
    SourceRef,
)
from domoai.runtime.executable_fingerprint import (
    capability_fingerprint,
    inventory_fingerprint,
)
from domoai.runtime.source_models import CapabilityRoute


def _device(*, maximum: int = 100, name: str = "Main Light") -> Device:
    return Device(
        id="living_room.main_light",
        type=DeviceType.LIGHT,
        name=name,
        area_id="living_room",
        protocol="fixture",
        capabilities=[
            Capability(
                name="brightness",
                kind=CapabilityKind.INTEGER,
                unit="%",
                readable=True,
                writable=True,
                minimum=0,
                maximum=maximum,
                commands=["set_brightness"],
            )
        ],
        availability=AvailabilityStatus.AVAILABLE,
        source_refs=[SourceRef(adapter_id="fixture", external_id="light.main")],
    )


def _route(*, available: bool = True, external_id: str = "light.main") -> CapabilityRoute:
    return CapabilityRoute(
        canonical_device_id="living_room.main_light",
        capability="brightness",
        source_ref=SourceRef(adapter_id="fixture", external_id=external_id),
        source_device_id="physical-light-1",
        local_canonical_id="living_room.main_light",
        commands=("set_brightness",),
        available=available,
    )


def test_executable_fingerprint_changes_for_bounds_and_route_availability() -> None:
    baseline = capability_fingerprint(_device(), _device().capabilities[0], [_route()])

    limited = _device(maximum=50)
    assert capability_fingerprint(limited, limited.capabilities[0], [_route()]) != baseline
    unavailable = _route(available=False)
    assert capability_fingerprint(_device(), _device().capabilities[0], [unavailable]) != baseline


def test_fingerprint_excludes_display_name_and_last_seen_timestamp() -> None:
    baseline = _device()
    renamed = baseline.model_copy(
        update={"name": "Renamed", "last_seen_at": datetime(2030, 1, 1, tzinfo=UTC)}
    )

    assert capability_fingerprint(baseline, baseline.capabilities[0], [_route()]) == (
        capability_fingerprint(renamed, renamed.capabilities[0], [_route()])
    )


def test_inventory_fingerprint_is_stable_for_reordered_equivalent_collections() -> None:
    device = _device()
    capability = device.capabilities[0].model_copy(
        update={"commands": ["set_brightness", "set_level"], "enum_values": ["high", "low"]}
    )
    source_refs = [
        SourceRef(adapter_id="fixture", external_id="light.main"),
        SourceRef(adapter_id="backup", external_id="light.backup"),
    ]
    device = device.model_copy(update={"capabilities": [capability], "source_refs": source_refs})
    routes = [_route(), _route(external_id="light.backup")]

    first = inventory_fingerprint(
        [device], {("living_room.main_light", "brightness"): routes}
    )
    reordered = device.model_copy(
        update={
            "capabilities": [
                device.capabilities[0].model_copy(
                    update={
                        "commands": ["set_level", "set_brightness"],
                        "enum_values": ["low", "high"],
                    }
                )
            ],
            "source_refs": list(reversed(device.source_refs)),
        }
    )
    second = inventory_fingerprint(
        [reordered], {("living_room.main_light", "brightness"): list(reversed(routes))}
    )

    assert first == second
