from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
BASE_COMPOSE = ROOT / "dev/lab/compose.yaml"
MOSQUITTO_CONFIG = ROOT / "dev/lab/mqtt/mosquitto.conf"
MQTT_CLIENTS = (
    ROOT / "dev/lab/mqtt/fake_zigbee2mqtt.py",
    ROOT / "dev/lab/battery/server.py",
    ROOT / "dev/lab/ev-charger/server.py",
    ROOT / "dev/lab/thermal/server.py",
    ROOT / "dev/lab/water-meter/server.py",
)


def _compose(path: Path) -> dict[str, object]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_base_mqtt_publication_is_loopback_only_and_has_no_bind_bypass() -> None:
    compose = _compose(BASE_COMPOSE)
    services = compose["services"]
    assert isinstance(services, dict)
    mqtt = services["mqtt"]
    assert isinstance(mqtt, dict)

    assert mqtt["ports"] == ["127.0.0.1:${DOMOAI_MQTT_HOST_PORT:-1883}:1883"]
    compose_text = BASE_COMPOSE.read_text(encoding="utf-8")
    assert "DOMOAI_MQTT_HOST_BIND" not in compose_text
    assert '"1883:1883"' not in compose_text


def test_base_mqtt_requires_authentication_and_separate_credential_volume() -> None:
    compose = _compose(BASE_COMPOSE)
    services = compose["services"]
    assert isinstance(services, dict)
    mqtt = services["mqtt"]
    assert isinstance(mqtt, dict)

    assert "mqtt-auth:/mosquitto/auth" in mqtt["volumes"]
    config = MOSQUITTO_CONFIG.read_text(encoding="utf-8")
    assert "allow_anonymous false" in config
    assert "password_file /mosquitto/auth/passwd" in config
    assert "listener 1883 0.0.0.0" in config


def test_base_mqtt_credentials_are_required_and_propagated_to_every_client() -> None:
    compose = _compose(BASE_COMPOSE)
    services = compose["services"]
    assert isinstance(services, dict)
    for service_name in ("mqtt", "zigbee2mqtt", "battery", "ev-charger", "thermal", "water-meter"):
        service = services[service_name]
        assert isinstance(service, dict), service_name
        environment = service["environment"]
        assert isinstance(environment, dict), service_name
        keys = (
            ("DOMOAI_MQTT_USERNAME", "DOMOAI_MQTT_PASSWORD")
            if service_name == "mqtt"
            else ("MQTT_USERNAME", "MQTT_PASSWORD")
        )
        for key in keys:
            assert ":?" in str(environment[key]), (service_name, key)

    for client_path in MQTT_CLIENTS:
        client_source = client_path.read_text(encoding="utf-8")
        assert "username_pw_set" in client_source, client_path
        assert 'os.getenv("MQTT_USERNAME"' in client_source, client_path
        assert 'os.getenv("MQTT_PASSWORD"' in client_source, client_path


def test_broker_healthcheck_authenticates_instead_of_using_anonymous_probe() -> None:
    compose = _compose(BASE_COMPOSE)
    services = compose["services"]
    assert isinstance(services, dict)
    mqtt = services["mqtt"]
    assert isinstance(mqtt, dict)
    healthcheck = mqtt["healthcheck"]
    assert isinstance(healthcheck, dict)
    test = healthcheck["test"]
    assert isinstance(test, list)
    rendered = " ".join(str(item) for item in test)
    assert "-u" in rendered
    assert "-P" in rendered


def test_shared_tls_overlay_is_explicit_and_never_publishes_remote_plaintext() -> None:
    overlay_path = ROOT / "dev/lab/compose.shared-mqtt-tls.yaml"
    config_path = ROOT / "dev/lab/mqtt/mosquitto.shared.conf"
    assert overlay_path.is_file()
    assert config_path.is_file()
    overlay = _compose(overlay_path)
    services = overlay["services"]
    assert isinstance(services, dict)
    mqtt = services["mqtt"]
    assert isinstance(mqtt, dict)
    ports = mqtt["ports"]
    assert ports == [
        "127.0.0.1:${DOMOAI_MQTT_HOST_PORT:-1883}:1883",
        (
            "${DOMOAI_MQTT_TLS_BIND:?DOMOAI_MQTT_TLS_BIND is required}:"
            "${DOMOAI_MQTT_TLS_HOST_PORT:-8883}:8883"
        ),
    ]
    overlay_text = overlay_path.read_text(encoding="utf-8")
    assert "DOMOAI_MQTT_TLS_CERT_FILE:?" in overlay_text
    assert "DOMOAI_MQTT_TLS_KEY_FILE:?" in overlay_text
    assert "test -f /mosquitto/certs-input/server.crt" in overlay_text
    assert "test -f /mosquitto/certs-input/server.key" in overlay_text
    config = config_path.read_text(encoding="utf-8")
    assert "listener 8883 0.0.0.0" in config
    assert "allow_anonymous false" in config
    assert "certfile /mosquitto/certs/server.crt" in config
    assert "keyfile /mosquitto/certs/server.key" in config
