from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.config.settings import Settings


def test_zigbee2mqtt_settings_are_loaded_with_secret_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_ZIGBEE2MQTT_URL", "mqtt://broker.test:1884")
    monkeypatch.setenv("DOMOAI_ZIGBEE2MQTT_BASE_TOPIC", "z2m")
    monkeypatch.setenv("DOMOAI_MQTT_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("DOMOAI_MQTT_USERNAME", "domoai")
    monkeypatch.setenv("DOMOAI_MQTT_PASSWORD", "secret-token")

    settings = Settings.from_environment()

    assert settings.zigbee2mqtt_url == "mqtt://broker.test:1884"
    assert settings.zigbee2mqtt_base_topic == "z2m"
    assert settings.mqtt_timeout_seconds == 7
    assert settings.mqtt_password is not None
    assert "secret-token" not in repr(settings)


def test_operator_approval_token_defaults_to_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMOAI_OPERATOR_APPROVAL_TOKEN", raising=False)

    settings = Settings.from_environment()

    assert settings.operator_approval_token is None


def test_operator_approval_token_is_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_OPERATOR_APPROVAL_TOKEN", "let-me-in")

    settings = Settings.from_environment()

    assert settings.operator_approval_token is not None
    assert settings.operator_approval_token.get_secret_value() == "let-me-in"
    assert "let-me-in" not in repr(settings)


def test_settings_allow_multiple_live_sources() -> None:
    settings = Settings(
        home_assistant_url="http://home-assistant.test",
        home_assistant_token=SecretStr("fixture-token"),
        zigbee2mqtt_url="mqtt://broker.test:1883",
    )

    assert settings.home_assistant_url is not None
    assert settings.zigbee2mqtt_url is not None


def test_home_assistant_provider_mode_requires_explicit_connection_pair() -> None:
    with pytest.raises(ValueError, match="DOMOAI_HOME_ASSISTANT_PROVIDER"):
        Settings(home_assistant_provider=True)

    with pytest.raises(ValueError, match="DOMOAI_HOME_ASSISTANT_MAPPING_PATH"):
        Settings(home_assistant_mapping_path=Path("config/home-assistant-mappings.json"))


def test_home_assistant_provider_settings_are_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_HOME_ASSISTANT_PROVIDER", "1")
    monkeypatch.setenv("DOMOAI_HOME_ASSISTANT_URL", "http://home-assistant.test")
    monkeypatch.setenv("DOMOAI_HOME_ASSISTANT_TOKEN", "fixture-token")
    monkeypatch.setenv(
        "DOMOAI_HOME_ASSISTANT_MAPPING_PATH", "config/home-assistant-mappings.json"
    )

    settings = Settings.from_environment()

    assert settings.home_assistant_provider is True
    assert settings.home_assistant_mapping_path == Path(
        "config/home-assistant-mappings.json"
    )
    assert "fixture-token" not in repr(settings)


def test_matter_server_settings_are_loaded_with_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_MATTER_SERVER_URL", "ws://matter.test:5580/ws")
    monkeypatch.setenv("DOMOAI_MATTER_TIMEOUT_SECONDS", "8")

    settings = Settings.from_environment()

    assert settings.matter_server_url == "ws://matter.test:5580/ws"
    assert settings.matter_timeout_seconds == 8
    assert "matter.test" in repr(settings)


def test_settings_allow_matter_with_another_live_source() -> None:
    settings = Settings(
        matter_server_url="ws://matter.test:5580/ws",
        zigbee2mqtt_url="mqtt://broker.test:1883",
    )

    assert settings.matter_server_url is not None
    assert settings.zigbee2mqtt_url is not None


def test_modbus_settings_have_safe_defaults_and_require_complete_pair() -> None:
    settings = Settings(
        modbus_host="modbus.test", modbus_config_path=Path("config/modbus.json")
    )

    assert settings.modbus_port == 502
    assert settings.modbus_timeout_seconds == 5
    assert settings.modbus_poll_interval_seconds == 5

    with pytest.raises(ValueError, match="DOMOAI_MODBUS_CONFIG_PATH"):
        Settings(modbus_host="modbus.test")
    with pytest.raises(ValueError, match="DOMOAI_MODBUS_HOST"):
        Settings(modbus_config_path=Path("config/modbus.json"))


def test_modbus_settings_can_coexist_with_existing_sources() -> None:
    settings = Settings(
        modbus_host="modbus.test",
        modbus_config_path=Path("config/modbus.json"),
        knx_gateway_host="192.0.2.10",
        knx_config_path=Path("config/knx.json"),
        home_assistant_url="http://home-assistant.test",
        home_assistant_token=SecretStr("fixture-token"),
    )

    assert settings.modbus_host is not None
    assert settings.knx_gateway_host is not None


def test_mqtt_tls_settings_default_to_no_ca_and_verification_on() -> None:
    settings = Settings(zigbee2mqtt_url="mqtts://broker.test:8883")

    assert settings.mqtt_ca_cert_path is None
    assert settings.mqtt_client_cert_path is None
    assert settings.mqtt_client_key_path is None
    assert settings.mqtt_tls_insecure is False


def test_mqtt_client_cert_and_key_must_be_configured_together() -> None:
    with pytest.raises(ValueError, match="DOMOAI_MQTT_CLIENT_KEY_PATH"):
        Settings(mqtt_client_cert_path=Path("config/mqtt-client.crt"))

    with pytest.raises(ValueError, match="DOMOAI_MQTT_CLIENT_CERT_PATH"):
        Settings(mqtt_client_key_path=Path("config/mqtt-client.key"))


def test_mqtt_client_cert_and_key_pair_is_accepted_together() -> None:
    settings = Settings(
        mqtt_client_cert_path=Path("config/mqtt-client.crt"),
        mqtt_client_key_path=Path("config/mqtt-client.key"),
    )

    assert settings.mqtt_client_cert_path == Path("config/mqtt-client.crt")
    assert settings.mqtt_client_key_path == Path("config/mqtt-client.key")


def test_composite_event_queue_max_size_defaults_and_loads_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_settings = Settings()
    assert default_settings.composite_event_queue_max_size == 1000

    monkeypatch.setenv("DOMOAI_COMPOSITE_EVENT_QUEUE_MAX_SIZE", "42")
    settings = Settings.from_environment()

    assert settings.composite_event_queue_max_size == 42


def test_sqlite_busy_timeout_ms_defaults_and_loads_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_settings = Settings()
    assert default_settings.sqlite_busy_timeout_ms == 5000

    monkeypatch.setenv("DOMOAI_SQLITE_BUSY_TIMEOUT_MS", "250")
    settings = Settings.from_environment()

    assert settings.sqlite_busy_timeout_ms == 250


def test_mqtt_tls_settings_are_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_ZIGBEE2MQTT_URL", "mqtts://broker.test:8883")
    monkeypatch.setenv("DOMOAI_MQTT_CA_CERT_PATH", "config/mqtt-ca.pem")
    monkeypatch.setenv("DOMOAI_MQTT_CLIENT_CERT_PATH", "config/mqtt-client.crt")
    monkeypatch.setenv("DOMOAI_MQTT_CLIENT_KEY_PATH", "config/mqtt-client.key")
    monkeypatch.setenv("DOMOAI_MQTT_TLS_INSECURE", "true")

    settings = Settings.from_environment()

    assert settings.mqtt_ca_cert_path == Path("config/mqtt-ca.pem")
    assert settings.mqtt_client_cert_path == Path("config/mqtt-client.crt")
    assert settings.mqtt_client_key_path == Path("config/mqtt-client.key")
    assert settings.mqtt_tls_insecure is True


def test_energy_live_is_network_free_by_default() -> None:
    settings = Settings()

    assert settings.energy_live is False
    assert settings.tariff_provider is None
    assert settings.solar_provider is None


def test_energy_live_settings_load_explicit_omie_and_open_meteo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_ENERGY_LIVE", "1")
    monkeypatch.setenv("DOMOAI_TARIFF_PROVIDER", "omie")
    monkeypatch.setenv("DOMOAI_SOLAR_PROVIDER", "open_meteo")
    monkeypatch.setenv("DOMOAI_SOLAR_LAT", "40.4168")
    monkeypatch.setenv("DOMOAI_SOLAR_LON", "-3.7038")
    monkeypatch.setenv("DOMOAI_SOLAR_KWP", "6")
    monkeypatch.setenv("DOMOAI_SOLAR_TILT", "30")
    monkeypatch.setenv("DOMOAI_SOLAR_AZIMUTH", "0")
    monkeypatch.setenv("DOMOAI_SOLAR_PERFORMANCE_RATIO", "0.82")

    settings = Settings.from_environment()

    assert settings.energy_live is True
    assert settings.tariff_provider == "omie"
    assert settings.solar_provider == "open_meteo"
    assert settings.solar_installed_kwp == 6


def test_energy_live_settings_load_persistent_solar_profile_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMOAI_ENERGY_LIVE", "1")
    monkeypatch.setenv("DOMOAI_TARIFF_PROVIDER", "omie")
    monkeypatch.setenv("DOMOAI_SOLAR_PROVIDER", "open_meteo")
    monkeypatch.setenv("DOMOAI_SOLAR_PROFILE_PATH", "config/solar-profile.json")

    settings = Settings.from_environment()

    assert settings.solar_profile_path == Path("config/solar-profile.json")
    assert settings.solar_latitude is None


def test_settings_reject_profile_path_mixed_with_legacy_fields() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        Settings(
            solar_profile_path=Path("config/solar-profile.json"),
            solar_latitude=40.4168,
        )


def test_energy_live_requires_both_providers_and_solar_configuration() -> None:
    with pytest.raises(ValueError, match="DOMOAI_TARIFF_PROVIDER"):
        Settings(energy_live=True)

    with pytest.raises(ValueError, match="DOMOAI_SOLAR_LAT"):
        Settings(
            energy_live=True,
            tariff_provider="omie",
            solar_provider="open_meteo",
        )
