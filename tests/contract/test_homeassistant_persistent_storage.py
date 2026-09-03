from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_lab_homeassistant_uses_one_explicit_named_volume() -> None:
    compose = (ROOT / "dev/lab/compose.yaml").read_text(encoding="utf-8")
    service = compose[compose.index("  homeassistant:") : compose.index("  matter-server:")]

    assert "homeassistant-data:/config" in service
    assert "./homeassistant:/config" not in service
    assert "restart: unless-stopped" in service
    assert "name: domoai-lab-homeassistant-data" in compose
    assert "external: true" in compose


def test_lab_mqtt_retained_state_uses_one_explicit_named_volume() -> None:
    compose = (ROOT / "dev/lab/compose.yaml").read_text(encoding="utf-8")
    service = compose[compose.index("  mqtt:") : compose.index("  knx-gateway:")]
    mosquitto = (ROOT / "dev/lab/mqtt/mosquitto.conf").read_text(encoding="utf-8")

    assert "mqtt-data:/mosquitto/data" in service
    assert "name: domoai-lab-mqtt-data" in compose
    assert "persistence true" in mosquitto
    assert "persistence_location /mosquitto/data/" in mosquitto
    assert "autosave_interval 30" in mosquitto
    assert "autosave_on_changes true" in mosquitto
