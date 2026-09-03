from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

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


def test_live_lab_bootstrap_manifest_and_readiness_are_consistent() -> None:
    """Verify the operator quickstart against the running shared gateway."""

    if os.getenv("DOMOAI_LIVE_BOOTSTRAP_ENABLE") != "1":
        pytest.skip("set DOMOAI_LIVE_BOOTSTRAP_ENABLE=1 for the running lab gateway")

    manifest_path = Path(
        os.getenv("DOMOAI_BOOTSTRAP_MANIFEST_PATH", "data/runtime-bootstrap.json")
    )
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "v1"
    assert document["profile"] == "lab"
    assert document["candidates"]
    assert "DOMOAI_HOME_ASSISTANT_TOKEN" not in json.dumps(document)

    port = int(os.getenv("DOMOAI_MCP_PORT", "8124"))
    try:
        response = urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5)
    except HTTPError as error:
        if error.code != 503:
            raise
        response = error
    with response:
        payload = json.load(response)
    physical = payload["physical"]
    assert physical["battery_qualification"] in {
        "unsupported",
        "software-qualified",
        "hil-qualified",
    }
    if physical.get("battery_operational_status") == "observed-only":
        assert physical["battery_qualification"] == "unsupported"
