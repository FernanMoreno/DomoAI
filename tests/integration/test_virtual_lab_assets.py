from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from domoai.adapters.home_assistant.config import load_home_assistant_mapping
from domoai.adapters.knx.config import load_mapping as load_knx_mapping
from domoai.adapters.modbus.config import load_mapping as load_modbus_mapping
from domoai.config.battery_profile import load_dispatchable_battery_binding
from domoai.config.ev_charging_profile import load_ev_charging_binding
from domoai.lab.ev_charging_simulator import EVChargingSimulationProfile
from domoai.lab.runner import SERVICE_NAMES, SERVICE_PROFILES
from domoai.lab.thermal_simulator import ThermalSimulationProfile

ROOT = Path(__file__).parents[2]
LAB = ROOT / "dev" / "lab"


def _load_bridge_module() -> Any:
    path = LAB / "mqtt" / "fake_zigbee2mqtt.py"
    spec = importlib.util.spec_from_file_location("domoai_lab_fake_zigbee2mqtt", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_lab_mappings_are_valid_and_use_explicit_addresses() -> None:
    modbus = load_modbus_mapping(LAB / "configs" / "modbus.json")
    knx = load_knx_mapping(LAB / "configs" / "knx-virtual.json")
    battery_modbus = load_modbus_mapping(LAB / "configs" / "modbus-battery.json")
    battery_knx = load_knx_mapping(LAB / "configs" / "knx-battery-virtual.json")

    assert len(modbus.entities) >= 3
    assert len(knx.entities) >= 2
    assert battery_modbus.entities[0].semantic_type == "energy"
    assert battery_knx.entities[0].semantic_type == "energy"
    assert all(
        binding.state.address >= 0 for entity in modbus.entities for binding in entity.capabilities
    )
    assert all(
        "/" in binding.state_group_address
        for entity in knx.entities
        for binding in entity.capabilities
    )


def test_lab_battery_homeassistant_mapping_uses_stable_registry_identity() -> None:
    mapping = load_home_assistant_mapping(
        LAB / "configs" / "home-assistant-battery.json"
    )
    capacity = mapping.battery_capacity_bindings[
        "sensor.domoai_virtual_battery_virtual_battery_capacity"
    ]
    dispatch = mapping.battery_dispatch_bindings["lab-battery"]

    assert capacity.device_id is None
    assert dispatch.device_id is None
    assert capacity.identity_claims is not None
    assert dispatch.identity_claims == capacity.identity_claims
    assert capacity.identity_claims.identity_keys == ["mqtt:lab-battery-1"]


def test_lab_battery_dispatch_profile_is_a_complete_canonical_binding() -> None:
    binding = load_dispatchable_battery_binding(
        LAB / "configs" / "dispatchable-battery-lab.json"
    )

    assert binding.provider_id == "home_assistant"
    assert binding.device_id == "lab.battery"
    assert binding.profile.actuator is not None
    assert binding.profile.actuator.device_id == "lab.battery"
    assert binding.capacity_evidence.capacity_kwh == 10.0


def test_lab_water_meter_assets_are_valid_and_discoverable() -> None:
    # Spec 163 (mirrors Spec 161's analysis finding E1 pattern): assert real
    # loader success, proving discoverability, not just generic JSON shape.
    water_modbus = load_modbus_mapping(LAB / "configs" / "modbus-water-meter.json")
    water_ha = load_home_assistant_mapping(LAB / "configs" / "home-assistant-water-meter.json")

    assert water_modbus.entities[0].semantic_type == "energy"
    assert all(
        binding.command is None for binding in water_modbus.entities[0].capabilities
    )
    assert "sensor.domoai_virtual_water_meter_flow_rate" in water_ha.metric_mappings
    assert "water-meter" in SERVICE_NAMES
    assert SERVICE_PROFILES.get("water-meter") == "water-meter"


def test_lab_water_meter_profile_is_valid_simulation_profile() -> None:
    from domoai.lab.water_consumption_simulator import WaterConsumptionSimulationProfile

    payload = json.loads((LAB / "water-meter" / "profile.json").read_text(encoding="utf-8"))
    profile = WaterConsumptionSimulationProfile.from_dict(payload)

    assert profile.device_id == "lab-water-1"


def test_lab_ev_charger_assets_are_valid_and_discoverable() -> None:
    # Spec 162 (analysis finding E1): assert real loader success -- proves
    # discoverability through the existing generic adapter/provider
    # mapping-loading mechanism, not just generic JSON shape.
    profile = EVChargingSimulationProfile.from_dict(
        json.loads((LAB / "ev-charger" / "profile.json").read_text(encoding="utf-8"))
    )
    ev_modbus = load_modbus_mapping(LAB / "configs" / "modbus-ev-charger.json")
    ev_ha = load_home_assistant_mapping(LAB / "configs" / "home-assistant-ev-charger.json")
    combined_ha = load_home_assistant_mapping(LAB / "configs" / "home-assistant-lab.json")

    assert profile.device_id == "lab-ev-1"
    assert ev_modbus.entities[0].semantic_type == "energy"
    assert "sensor.domoai_virtual_ev_charger_virtual_ev_soc" in ev_ha.metric_mappings
    assert "binary_sensor.domoai_virtual_ev_charger_virtual_ev_connected" in ev_ha.metric_mappings
    assert combined_ha.metric_mappings["sensor.domoai_virtual_ev_charger_virtual_ev_soc"] == {
        "value": "ev.soc"
    }
    assert "lab-battery" in combined_ha.battery_dispatch_bindings
    ev_binding = load_ev_charging_binding(LAB / "configs" / "ev-charging-lab.json")
    assert ev_binding.provider_id == "home_assistant"
    assert ev_binding.device_id == "lab.ev_charger"
    assert ev_binding.actuator.max_charge_kw == 7.4
    assert "lab-ev" in combined_ha.ev_charging_bindings
    assert combined_ha.ev_charging_bindings["lab-ev"].canonical_device_id == "lab.ev_charger"
    assert "ev-charger" in SERVICE_NAMES
    assert SERVICE_PROFILES.get("ev-charger") == "ev-charger"


def test_lab_ev_modbus_server_preserves_function_code_address_spaces() -> None:
    source = (LAB / "ev-charger" / "server.py").read_text(encoding="utf-8")

    # The public EV mapping intentionally uses address 0 for both discrete
    # input and input-register state.  The simulator must therefore use
    # separate blocks and translate public addresses to its shared backing
    # store offsets explicitly.
    assert '"shared blocks": False' in source
    assert "_INPUT_REGISTER_OFFSET = _COIL_SIZE + _DISCRETE_INPUT_SIZE" in source
    assert "_HOLDING_REGISTER_OFFSET = _INPUT_REGISTER_OFFSET + _INPUT_REGISTER_SIZE" in source
    assert '"addr": [input_register_offset, input_register_offset + 1]' in source
    assert '"addr": [holding_register_offset + 10, holding_register_offset + 11]' in source
    assert "command_start = holding_register_offset + 10" in source
    assert "state_start = input_register_offset + address" in source


def test_lab_thermal_assets_are_valid_and_discoverable() -> None:
    # Spec 165 T023: assert real loader success, mirroring Spec 162/163's
    # analysis-finding-E1 pattern -- proves discoverability through the
    # existing generic adapter/provider mapping-loading mechanism, not just
    # generic JSON shape.
    profile = ThermalSimulationProfile.from_dict(
        json.loads((LAB / "thermal" / "profile.json").read_text(encoding="utf-8"))
    )
    thermal_modbus = load_modbus_mapping(LAB / "configs" / "modbus-thermal.json")
    thermal_ha = load_home_assistant_mapping(LAB / "configs" / "home-assistant-thermal.json")

    assert profile.device_id == "lab-thermostat-1"
    assert thermal_modbus.entities[0].semantic_type == "energy"
    assert "sensor.domoai_virtual_thermostat_indoor_temperature" in thermal_ha.metric_mappings
    assert "thermal" in SERVICE_NAMES
    assert SERVICE_PROFILES.get("thermal") == "thermal"


def test_lab_compose_references_only_local_secret_free_profiles() -> None:
    compose = (LAB / "compose.yaml").read_text(encoding="utf-8")
    example = (LAB / ".env.example").read_text(encoding="utf-8")

    for service in (
        "mqtt:",
        "zigbee2mqtt:",
        "modbus:",
        "battery:",
        "ev-charger:",
        "water-meter:",
        "thermal:",
        "knx-gateway:",
        "homeassistant:",
        "matter-server:",
    ):
        assert service in compose
    assert "allow_anonymous true" in (LAB / "mqtt" / "mosquitto.conf").read_text(encoding="utf-8")
    assert "<secret>" not in compose
    assert "<token>" not in compose
    assert "\nDOMOAI_MQTT_PASSWORD=" not in example


def test_experimental_knx_docker_gateway_cannot_autostart_alongside_wsl_gateway() -> None:
    compose = (LAB / "compose.yaml").read_text(encoding="utf-8")
    gateway = compose.split("  knx-gateway:", 1)[1].split("\n  zigbee2mqtt:", 1)[0]

    assert 'restart: "no"' in gateway


def test_fake_bridge_republishes_initial_state_and_accepts_bounded_set() -> None:
    module = _load_bridge_module()
    bridge = module.VirtualBridge()

    topics = {topic for topic, _payload, retained in bridge.initial_messages() if retained}
    assert bridge.topic("bridge/devices") in topics
    assert bridge.topic("living_room/main_light") in topics

    updated = bridge.apply_set("living_room/main_light", {"state": "OFF", "brightness": 80})
    assert updated == {"state": "OFF", "brightness": 80}
    assert bridge.apply_get("living_room/main_light", {"state": None}) == updated

    try:
        bridge.apply_set("living_room/main_light", {"vendor_command": "reset"})
    except ValueError as error:
        assert "unsupported" in str(error)
    else:  # pragma: no cover - assertion documents the safety boundary
        raise AssertionError("unknown virtual commands must be rejected")


def test_fake_bridge_accepts_paho_v2_reason_codes() -> None:
    module = _load_bridge_module()

    class Success:
        is_failure = False

    class Failure:
        is_failure = True

    assert module._connect_failed(Success()) is False
    assert module._connect_failed(Failure()) is True
    assert module._connect_failed(0) is False
    assert module._connect_failed(1) is True


def test_simulator_json_has_deterministic_tcp_endpoint() -> None:
    payload = json.loads((LAB / "modbus" / "simulator.json").read_text(encoding="utf-8"))
    server = payload["server_list"]["server"]
    device = payload["device_list"]["virtual_home"]
    assert server["comm"] == "tcp"
    assert server["port"] == 1502
    assert server["device_id"] == 1
    assert device["bits"][0]["addr"] == 0
