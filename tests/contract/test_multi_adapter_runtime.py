from __future__ import annotations

from domoai.domain.models import SourceRef
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.multi_adapter import source_snapshot


def test_registry_groups_entities_and_keeps_capability_routes() -> None:
    registry = DeviceRegistry()

    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")

    device = registry.get("living_room.main_light")
    assert device is not None
    assert {capability.name for capability in device.capabilities} == {"power", "brightness"}
    assert {ref.external_id for ref in device.source_refs} == {
        "light.main_power",
        "light.main_brightness",
    }
    power_route = registry.resolve_command_route("living_room.main_light", "turn_on")
    brightness_route = registry.resolve_command_route("living_room.main_light", "set_brightness")
    assert power_route.route is not None
    assert power_route.route.source_ref == SourceRef(
        adapter_id="home_assistant", external_id="light.main_power", external_type="light"
    )
    assert brightness_route.route is not None
    assert brightness_route.route.source_ref.external_id == "light.main_brightness"


def test_explicit_canonical_link_merges_only_matching_source_contributions() -> None:
    registry = DeviceRegistry()

    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")
    registry.apply_snapshot(source_snapshot(adapter_id="modbus"), "modbus")

    device = registry.get("living_room.main_light")
    assert device is not None
    assert {ref.adapter_id for ref in device.source_refs} == {"home_assistant", "modbus"}

    unrelated = registry.get("modbus.environment")
    assert unrelated is not None


def test_ambiguous_capability_route_is_reported_before_write() -> None:
    registry = DeviceRegistry()

    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")
    registry.apply_snapshot(source_snapshot(adapter_id="modbus"), "modbus")

    resolution = registry.resolve_command_route("living_room.main_light", "set_brightness")
    assert resolution.route is None
    assert resolution.reason == "ambiguous_route"
    assert len(resolution.candidates) == 2


def test_legacy_source_ids_remain_resolvable() -> None:
    registry = DeviceRegistry()
    snapshot = source_snapshot(adapter_id="fixture")
    for item in snapshot.source_entities:
        item.pop("canonical_id", None)
        item.pop("identity_keys", None)
        item.pop("connections", None)

    registry.apply_snapshot(snapshot, "fixture")
    source_id = registry.canonical_id_for_source("fixture", "light.main_power")
    assert source_id is not None
    assert registry.resolve_command_route(source_id, "turn_on").route is not None


def test_unknown_source_reference_is_not_accepted_by_public_lookup() -> None:
    registry = DeviceRegistry()
    registry.apply_snapshot(source_snapshot(adapter_id="home_assistant"), "home_assistant")

    assert registry.canonical_id_for_source("home_assistant", "raw.register.1") is None
    assert registry.resolve_command_route("unknown.device", "turn_on").route is None


def test_identity_keys_keep_source_device_stable_and_preserve_parent_topology() -> None:
    registry = DeviceRegistry()
    snapshot = source_snapshot(adapter_id="home_assistant")
    for item in snapshot.source_entities:
        if item["entity_id"].startswith("light.main_"):
            item.pop("canonical_id", None)
            item["parent_device_id"] = "ha-hub-1"
    registry.apply_snapshot(snapshot, "home_assistant")

    renamed = source_snapshot(adapter_id="home_assistant")
    for item in renamed.source_entities:
        if item["entity_id"] == "light.main_power":
            item.pop("canonical_id", None)
            item["name"] = "Renamed Main Light"
            item["area_id"] = "kitchen"
        elif item["entity_id"] == "light.main_brightness":
            item.pop("canonical_id", None)
            item["name"] = "Renamed Main Light Brightness"
            item["area_id"] = "kitchen"
    registry.apply_snapshot(renamed, "home_assistant")

    assert registry.canonical_id_for_source("home_assistant", "light.main_power") == (
        "living_room.main-light"
    )
    assert registry.parent_source_device_for("home_assistant", "physical-light-1") == ("ha-hub-1")
