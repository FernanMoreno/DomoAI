from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _compose(path: Path) -> dict[str, object]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_base_lab_mqtt_contract_is_loopback_authenticated_and_separate_from_state() -> None:
    compose = _compose(ROOT / "dev/lab/compose.yaml")
    services = compose["services"]
    assert isinstance(services, dict)
    mqtt = services["mqtt"]
    assert isinstance(mqtt, dict)

    assert mqtt["ports"] == ["127.0.0.1:${DOMOAI_MQTT_HOST_PORT:-1883}:1883"]
    assert "mqtt-data:/mosquitto/data" in mqtt["volumes"]
    assert "mqtt-auth:/mosquitto/auth" in mqtt["volumes"]
    compose_text = (ROOT / "dev/lab/compose.yaml").read_text(encoding="utf-8")
    assert "name: domoai-lab-mqtt-data" in compose_text
    assert "name: domoai-lab-mqtt-auth" in compose_text


def test_shared_mqtt_contract_requires_tls_material_and_preserves_local_plaintext_only() -> None:
    overlay_path = ROOT / "dev/lab/compose.shared-mqtt-tls.yaml"
    overlay = _compose(overlay_path)
    services = overlay["services"]
    assert isinstance(services, dict)
    mqtt = services["mqtt"]
    assert isinstance(mqtt, dict)
    volumes = "\n".join(str(item) for item in mqtt["volumes"])
    assert "DOMOAI_MQTT_TLS_CERT_FILE:?" in volumes
    assert "DOMOAI_MQTT_TLS_KEY_FILE:?" in volumes
    assert "server.crt" in volumes
    assert "server.key" in volumes
    assert "8883" in "\n".join(str(item) for item in mqtt["ports"])
    assert all("0.0.0.0:" not in str(item) for item in mqtt["ports"])


def test_documented_lab_env_requires_mqtt_credentials_without_committing_values() -> None:
    env_example = (ROOT / "dev/lab/.env.example").read_text(encoding="utf-8")
    assert "DOMOAI_MQTT_USERNAME=" in env_example
    assert "DOMOAI_MQTT_PASSWORD=" in env_example
    assert "<secret>" not in env_example
    assert "DOMOAI_MQTT_TLS_BIND=" in env_example
    assert "DOMOAI_MQTT_TLS_CERT_FILE=" in env_example
    assert "DOMOAI_MQTT_TLS_KEY_FILE=" in env_example
