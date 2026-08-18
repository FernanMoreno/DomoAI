"""Environment-backed settings with secret-safe representation."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, SecretStr, model_validator

from domoai.domain.models import StrictModel


class Settings(StrictModel):
    database_path: Path = Path("data/domoai.sqlite3")
    policy_config_path: Path | None = None
    risk_overrides_path: Path | None = None
    home_assistant_url: str | None = None
    home_assistant_token: SecretStr | None = None
    home_assistant_provider: bool = False
    home_assistant_mapping_path: Path | None = None
    zigbee2mqtt_url: str | None = None
    zigbee2mqtt_base_topic: str = Field(default="zigbee2mqtt", min_length=1)
    matter_server_url: str | None = None
    knx_gateway_host: str | None = None
    knx_config_path: Path | None = None
    knx_timeout_seconds: float = Field(default=5.0, gt=0)
    modbus_host: str | None = None
    modbus_port: int = Field(default=502, ge=1, le=65535)
    modbus_config_path: Path | None = None
    modbus_timeout_seconds: float = Field(default=5.0, gt=0)
    modbus_poll_interval_seconds: float = Field(default=5.0, gt=0)
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None
    mqtt_timeout_seconds: float = Field(default=5.0, gt=0)
    mqtt_ca_cert_path: Path | None = None
    mqtt_client_cert_path: Path | None = None
    mqtt_client_key_path: Path | None = None
    mqtt_tls_insecure: bool = False
    matter_timeout_seconds: float = Field(default=5.0, gt=0)
    state_stale_after_seconds: int = 300
    scheduler_poll_interval_seconds: int = Field(default=30, gt=0)
    scheduler_grace_window_seconds: int = Field(default=900, gt=0)
    energy_live: bool = False
    tariff_provider: str | None = None
    solar_provider: str | None = None
    omie_timeout_seconds: float = Field(default=10.0, gt=0)
    solar_latitude: float | None = Field(default=None, ge=-90, le=90)
    solar_longitude: float | None = Field(default=None, ge=-180, le=180)
    solar_installed_kwp: float | None = Field(default=None, gt=0)
    solar_tilt: float | None = Field(default=None, ge=0, le=90)
    solar_azimuth: float | None = Field(default=None, ge=-180, le=180)
    solar_performance_ratio: float | None = Field(default=None, gt=0, le=1)
    solar_inverter_ac_max_kw: float | None = Field(default=None, gt=0)
    solar_timezone: str = Field(default="Europe/Madrid", min_length=1)
    solar_profile_path: Path | None = None
    solar_timeout_seconds: float = Field(default=10.0, gt=0)
    energy_max_age_seconds: float | None = Field(default=900.0, ge=0)

    @model_validator(mode="after")
    def validate_source_selection(self) -> Settings:
        if self.home_assistant_provider and (
            self.home_assistant_url is None or self.home_assistant_token is None
        ):
            raise ValueError(
                "DOMOAI_HOME_ASSISTANT_PROVIDER requires "
                "DOMOAI_HOME_ASSISTANT_URL and DOMOAI_HOME_ASSISTANT_TOKEN"
            )
        if self.home_assistant_mapping_path is not None and not self.home_assistant_provider:
            raise ValueError(
                "DOMOAI_HOME_ASSISTANT_MAPPING_PATH requires DOMOAI_HOME_ASSISTANT_PROVIDER"
            )
        knx_settings = (self.knx_gateway_host is not None, self.knx_config_path is not None)
        if knx_settings[0] != knx_settings[1]:
            raise ValueError(
                "DOMOAI_KNX_GATEWAY_HOST and DOMOAI_KNX_CONFIG_PATH must be configured together"
            )
        modbus_settings = (self.modbus_host is not None, self.modbus_config_path is not None)
        if modbus_settings[0] != modbus_settings[1]:
            raise ValueError(
                "DOMOAI_MODBUS_HOST and DOMOAI_MODBUS_CONFIG_PATH must be configured together"
            )
        if self.mqtt_password is not None and self.mqtt_username is None:
            raise ValueError("DOMOAI_MQTT_USERNAME is required when a password is configured")
        mqtt_client_cert_settings = (
            self.mqtt_client_cert_path is not None,
            self.mqtt_client_key_path is not None,
        )
        if mqtt_client_cert_settings[0] != mqtt_client_cert_settings[1]:
            raise ValueError(
                "DOMOAI_MQTT_CLIENT_CERT_PATH and DOMOAI_MQTT_CLIENT_KEY_PATH "
                "must be configured together"
            )
        legacy_solar_values = (
            self.solar_latitude,
            self.solar_longitude,
            self.solar_installed_kwp,
            self.solar_tilt,
            self.solar_azimuth,
            self.solar_performance_ratio,
            self.solar_inverter_ac_max_kw,
        )
        if self.solar_profile_path is not None and any(
            value is not None for value in legacy_solar_values
        ):
            raise ValueError(
                "DOMOAI_SOLAR_PROFILE_PATH cannot be combined with legacy solar fields"
            )
        if self.energy_live:
            if self.tariff_provider != "omie":
                raise ValueError(
                    "DOMOAI_TARIFF_PROVIDER must be 'omie' when energy live mode is enabled"
                )
            if self.solar_provider != "open_meteo":
                raise ValueError(
                    "DOMOAI_SOLAR_PROVIDER must be 'open_meteo' when energy live mode is enabled"
                )
            if self.solar_profile_path is None:
                required_solar = {
                    "DOMOAI_SOLAR_LAT": self.solar_latitude,
                    "DOMOAI_SOLAR_LON": self.solar_longitude,
                    "DOMOAI_SOLAR_KWP": self.solar_installed_kwp,
                    "DOMOAI_SOLAR_TILT": self.solar_tilt,
                    "DOMOAI_SOLAR_AZIMUTH": self.solar_azimuth,
                    "DOMOAI_SOLAR_PERFORMANCE_RATIO": self.solar_performance_ratio,
                }
                missing = [name for name, value in required_solar.items() if value is None]
                if missing:
                    raise ValueError(
                        "missing live solar configuration: " + ", ".join(missing)
                    )
        return self

    @classmethod
    def from_environment(cls) -> Settings:
        stale_after = os.getenv("DOMOAI_STATE_STALE_AFTER_SECONDS", "300")

        def optional_float(name: str) -> float | None:
            value = os.getenv(name)
            return float(value) if value is not None else None

        def boolean(name: str, default: bool = False) -> bool:
            value = os.getenv(name)
            if value is None:
                return default
            if value.strip().lower() in {"1", "true", "yes", "on"}:
                return True
            if value.strip().lower() in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{name} must be a boolean")

        return cls(
            database_path=Path(os.getenv("DOMOAI_DATABASE_PATH", "data/domoai.sqlite3")),
            policy_config_path=(
                Path(policy_path)
                if (policy_path := os.getenv("DOMOAI_POLICY_CONFIG_PATH"))
                else None
            ),
            risk_overrides_path=(
                Path(risk_path)
                if (risk_path := os.getenv("DOMOAI_RISK_OVERRIDES_PATH"))
                else None
            ),
            home_assistant_url=os.getenv("DOMOAI_HOME_ASSISTANT_URL"),
            home_assistant_token=(
                SecretStr(token) if (token := os.getenv("DOMOAI_HOME_ASSISTANT_TOKEN")) else None
            ),
            home_assistant_provider=boolean("DOMOAI_HOME_ASSISTANT_PROVIDER"),
            home_assistant_mapping_path=(
                Path(mapping_path)
                if (mapping_path := os.getenv("DOMOAI_HOME_ASSISTANT_MAPPING_PATH"))
                else None
            ),
            zigbee2mqtt_url=os.getenv("DOMOAI_ZIGBEE2MQTT_URL"),
            zigbee2mqtt_base_topic=os.getenv(
                "DOMOAI_ZIGBEE2MQTT_BASE_TOPIC", "zigbee2mqtt"
            ),
            matter_server_url=os.getenv("DOMOAI_MATTER_SERVER_URL"),
            knx_gateway_host=os.getenv("DOMOAI_KNX_GATEWAY_HOST"),
            knx_config_path=(
                Path(config_path)
                if (config_path := os.getenv("DOMOAI_KNX_CONFIG_PATH"))
                else None
            ),
            knx_timeout_seconds=float(os.getenv("DOMOAI_KNX_TIMEOUT_SECONDS", "5")),
            modbus_host=os.getenv("DOMOAI_MODBUS_HOST"),
            modbus_port=int(os.getenv("DOMOAI_MODBUS_PORT", "502")),
            modbus_config_path=(
                Path(config_path)
                if (config_path := os.getenv("DOMOAI_MODBUS_CONFIG_PATH"))
                else None
            ),
            modbus_timeout_seconds=float(os.getenv("DOMOAI_MODBUS_TIMEOUT_SECONDS", "5")),
            modbus_poll_interval_seconds=float(
                os.getenv("DOMOAI_MODBUS_POLL_INTERVAL_SECONDS", "5")
            ),
            mqtt_username=os.getenv("DOMOAI_MQTT_USERNAME"),
            mqtt_password=(
                SecretStr(password)
                if (password := os.getenv("DOMOAI_MQTT_PASSWORD"))
                else None
            ),
            mqtt_timeout_seconds=float(os.getenv("DOMOAI_MQTT_TIMEOUT_SECONDS", "5")),
            mqtt_ca_cert_path=(
                Path(ca_path)
                if (ca_path := os.getenv("DOMOAI_MQTT_CA_CERT_PATH"))
                else None
            ),
            mqtt_client_cert_path=(
                Path(cert_path)
                if (cert_path := os.getenv("DOMOAI_MQTT_CLIENT_CERT_PATH"))
                else None
            ),
            mqtt_client_key_path=(
                Path(key_path)
                if (key_path := os.getenv("DOMOAI_MQTT_CLIENT_KEY_PATH"))
                else None
            ),
            mqtt_tls_insecure=boolean("DOMOAI_MQTT_TLS_INSECURE"),
            matter_timeout_seconds=float(os.getenv("DOMOAI_MATTER_TIMEOUT_SECONDS", "5")),
            state_stale_after_seconds=int(stale_after),
            energy_live=boolean("DOMOAI_ENERGY_LIVE"),
            tariff_provider=os.getenv("DOMOAI_TARIFF_PROVIDER"),
            solar_provider=os.getenv("DOMOAI_SOLAR_PROVIDER"),
            omie_timeout_seconds=float(os.getenv("DOMOAI_OMIE_TIMEOUT_SECONDS", "10")),
            solar_latitude=optional_float("DOMOAI_SOLAR_LAT"),
            solar_longitude=optional_float("DOMOAI_SOLAR_LON"),
            solar_installed_kwp=optional_float("DOMOAI_SOLAR_KWP"),
            solar_tilt=optional_float("DOMOAI_SOLAR_TILT"),
            solar_azimuth=optional_float("DOMOAI_SOLAR_AZIMUTH"),
            solar_performance_ratio=optional_float("DOMOAI_SOLAR_PERFORMANCE_RATIO"),
            solar_inverter_ac_max_kw=optional_float("DOMOAI_SOLAR_INVERTER_AC_MAX_KW"),
            solar_timezone=os.getenv("DOMOAI_SOLAR_TIMEZONE", "Europe/Madrid"),
            solar_profile_path=(
                Path(profile_path)
                if (profile_path := os.getenv("DOMOAI_SOLAR_PROFILE_PATH"))
                else None
            ),
            solar_timeout_seconds=float(os.getenv("DOMOAI_SOLAR_TIMEOUT_SECONDS", "10")),
            energy_max_age_seconds=optional_float("DOMOAI_ENERGY_MAX_AGE_SECONDS")
            if os.getenv("DOMOAI_ENERGY_MAX_AGE_SECONDS") is not None
            else 900.0,
        )
