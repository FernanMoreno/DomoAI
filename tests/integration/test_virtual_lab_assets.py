from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from domoai.adapters.knx.config import load_mapping as load_knx_mapping
from domoai.adapters.modbus.config import load_mapping as load_modbus_mapping

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

    assert len(modbus.entities) >= 3
    assert len(knx.entities) >= 2
    assert all(
        binding.state.address >= 0 for entity in modbus.entities for binding in entity.capabilities
    )
    assert all(
        "/" in binding.state_group_address
        for entity in knx.entities
        for binding in entity.capabilities
    )


def test_lab_compose_references_only_local_secret_free_profiles() -> None:
    compose = (LAB / "compose.yaml").read_text(encoding="utf-8")
    example = (LAB / ".env.example").read_text(encoding="utf-8")

    for service in ("mqtt:", "zigbee2mqtt:", "modbus:", "homeassistant:", "matter-server:"):
        assert service in compose
    assert "allow_anonymous true" in (LAB / "mqtt" / "mosquitto.conf").read_text(encoding="utf-8")
    assert "<secret>" not in compose
    assert "<token>" not in compose
    assert "\nDOMOAI_MQTT_PASSWORD=" not in example


def test_fake_bridge_republishes_initial_state_and_accepts_bounded_set() -> None:
    module = _load_bridge_module()
    bridge = module.VirtualBridge()

    topics = {topic for topic, _payload, retained in bridge.initial_messages() if retained}
    assert bridge.topic("bridge/devices") in topics
    assert bridge.topic("living_room/main_light") in topics

    updated = bridge.apply_set("living_room/main_light", {"state": "OFF", "brightness": 80})
    assert updated == {"state": "OFF", "brightness": 80}

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
