from __future__ import annotations

from domoai.domain.models import AdapterSnapshot
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.multi_adapter import entity, power_capability, source_snapshot


def test_two_distinct_identities_colliding_on_the_same_generated_id_get_distinct_ids() -> None:
    registry = DeviceRegistry()
    colliding_entities = [
        {
            "entity_id": "light.first",
            "device_id": "device-first",
            "identity_keys": ["fixture:device:device-first"],
            "connections": [],
            "name": "Main Light",
            "area_id": "living_room",
            "domain": "light",
            "semantic_type": "light",
            "capabilities": [power_capability()],
            "available": True,
        },
        {
            "entity_id": "light.second",
            "device_id": "device-second",
            "identity_keys": ["fixture:device:device-second"],
            "connections": [],
            "name": "Main Light",
            "area_id": "living_room",
            "domain": "light",
            "semantic_type": "light",
            "capabilities": [power_capability()],
            "available": True,
        },
    ]
    snapshot = AdapterSnapshot(source_entities=colliding_entities, source_states=[])

    registry.apply_snapshot(snapshot, "fixture")

    first_id = registry.canonical_id_for_source("fixture", "light.first")
    second_id = registry.canonical_id_for_source("fixture", "light.second")
    assert first_id is not None
    assert second_id is not None
    assert first_id != second_id
    assert first_id == "living_room.main-light"
    assert second_id == "living_room.main-light-2"


def test_device_removed_when_no_longer_reported() -> None:
    registry = DeviceRegistry()
    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")
    assert registry.get("living_room.main_light") is not None

    registry.apply_snapshot(
        source_snapshot(adapter_id="home_assistant", include_shared_device=False),
        "home_assistant",
    )

    assert registry.get("living_room.main_light") is None
    resolution = registry.resolve_command_route("living_room.main_light", "turn_on")
    assert resolution.route is None
    assert resolution.reason == "device_not_found"


def test_disconnected_adapter_never_reconciled() -> None:
    registry = DeviceRegistry()
    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")
    assert registry.get("living_room.main_light") is not None

    failed_snapshot = AdapterSnapshot(
        source_entities=[],
        source_states=[],
        unsupported_sources=[{"adapter_id": "home_assistant", "failure": True}],
    )
    registry.apply_snapshot(failed_snapshot, "home_assistant")

    assert registry.get("living_room.main_light") is not None
    assert registry.get("home_assistant.environment") is not None


def test_multi_adapter_device_survives_losing_one_adapter() -> None:
    registry = DeviceRegistry()
    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")
    registry.apply_snapshot(source_snapshot(adapter_id="modbus"), "modbus")

    device = registry.get("living_room.main_light")
    assert device is not None
    assert {ref.adapter_id for ref in device.source_refs} == {"home_assistant", "modbus"}

    registry.apply_snapshot(
        source_snapshot(adapter_id="home_assistant", include_shared_device=False),
        "home_assistant",
    )

    device_after = registry.get("living_room.main_light")
    assert device_after is not None
    assert {ref.adapter_id for ref in device_after.source_refs} == {"modbus"}
    route = registry.resolve_command_route("living_room.main_light", "turn_on")
    assert route.route is not None
    assert route.route.source_ref.adapter_id == "modbus"


def test_reappearance_recovers_same_canonical_id() -> None:
    registry = DeviceRegistry()

    def snapshot_without_canonical_id(**kwargs: object) -> AdapterSnapshot:
        snapshot = source_snapshot(adapter_id="fixture", **kwargs)  # type: ignore[arg-type]
        for item in snapshot.source_entities:
            item.pop("canonical_id", None)
        return snapshot

    registry.apply_snapshot(snapshot_without_canonical_id(), "fixture")
    original_id = registry.canonical_id_for_source("fixture", "light.main_power")
    assert original_id is not None
    assert registry.get(original_id) is not None

    registry.apply_snapshot(snapshot_without_canonical_id(include_shared_device=False), "fixture")
    assert registry.get(original_id) is None

    registry.apply_snapshot(snapshot_without_canonical_id(), "fixture")

    reappeared_id = registry.canonical_id_for_source("fixture", "light.main_power")
    assert reappeared_id == original_id
    assert registry.get(original_id) is not None


def test_drain_diagnostics_returns_and_clears_accumulated_diagnostics() -> None:
    registry = DeviceRegistry()
    first = entity(
        entity_id="conflict.a",
        source_device_id="conflict-device",
        canonical_id="conflict.device",
        name="Conflict A",
        capabilities=[power_capability()],
    )
    second = entity(
        entity_id="conflict.b",
        source_device_id="conflict-device",
        canonical_id="conflict.device",
        name="Conflict B",
        capabilities=[power_capability()],
    )
    second["semantic_type"] = "sensor"
    registry.apply_snapshot(AdapterSnapshot(source_entities=[first]), "fixture")
    registry.apply_snapshot(AdapterSnapshot(source_entities=[second]), "fixture")

    assert len(registry.diagnostics) == 1
    assert registry.diagnostics[0]["kind"] == "canonical_type_conflict"

    drained = registry.drain_diagnostics()
    assert len(drained) == 1
    assert drained[0]["kind"] == "canonical_type_conflict"
    assert registry.diagnostics == []
    assert registry.drain_diagnostics() == []


def test_same_source_capability_metadata_refresh_replaces_previous_value() -> None:
    registry = DeviceRegistry()
    first = entity(
        entity_id="light.brightness",
        source_device_id="brightness-device",
        canonical_id="living_room.brightness",
        name="Brightness",
        capabilities=[
            {
                **power_capability(),
                "name": "brightness",
                "kind": "integer",
                "unit": "%",
                "minimum": 0,
                "maximum": 100,
                "commands": ["set_brightness"],
            }
        ],
    )
    second = {
        **first,
        "domain": "light-v2",
        "capabilities": [{**first["capabilities"][0], "maximum": 50}],
    }

    registry.apply_snapshot(AdapterSnapshot(source_entities=[first]), "fixture")
    registry.apply_snapshot(AdapterSnapshot(source_entities=[second]), "fixture")

    device = registry.get("living_room.brightness")
    assert device is not None
    assert device.capabilities[0].maximum == 50
    routes = registry.routes_for("living_room.brightness", "brightness")
    assert len(routes) == 1
    assert routes[0].source_ref.external_type == "light-v2"
    assert registry.diagnostics == []
