from __future__ import annotations

from pathlib import Path

import pytest

from domoai.adapters.modbus.config import load_mapping
from domoai.adapters.zigbee2mqtt.transport import AiomqttTransport
from tests.integration.test_zigbee2mqtt_smoke import build_live_adapter

ROOT = Path(__file__).parents[2]


def test_zigbee_smoke_wires_lab_endpoint_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_MQTT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("DOMOAI_ZIGBEE2MQTT_BASE_TOPIC", "lab-zigbee")
    monkeypatch.setenv("DOMOAI_MQTT_USERNAME", "lab-user")
    monkeypatch.setenv("DOMOAI_MQTT_PASSWORD", "lab-password")

    adapter = build_live_adapter("mqtt://127.0.0.1:1884")
    assert adapter.base_topic == "lab-zigbee"
    assert isinstance(adapter.transport, AiomqttTransport)
    assert adapter.transport.host == "127.0.0.1"
    assert adapter.transport.port == 1884
    assert adapter.transport.username == "lab-user"
    assert adapter.transport.password == "lab-password"
    assert adapter.transport.timeout == 2


def test_modbus_lab_mapping_points_to_the_simulator_contract() -> None:
    mapping = load_mapping(ROOT / "dev" / "lab" / "configs" / "modbus.json")
    points = {
        (entity.unit_id, capability.state.area, capability.state.address)
        for entity in mapping.entities
        for capability in entity.capabilities
    }
    assert (1, "coil", 0) in points
    assert (1, "holding_register", 10) in points
    assert (1, "input_register", 20) in points
    assert (1, "input_register", 21) in points
