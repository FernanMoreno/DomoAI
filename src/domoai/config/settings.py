"""Environment-backed settings with secret-safe representation."""

from __future__ import annotations

import os
from ipaddress import ip_address
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator

from domoai.domain.models import StrictModel


class Settings(StrictModel):
    bootstrap_profile: Literal["none", "lab"] = "none"
    bootstrap_manifest_path: Path | None = None
    commissioning_manifest_path: Path | None = None
    mcp_host: str = Field(default="127.0.0.1", min_length=1)
    mcp_port: int = Field(default=8000, ge=1, le=65535)
    mcp_path: str = Field(default="/mcp", min_length=2)
    mcp_public_url: str = "http://127.0.0.1:8000"
    mcp_client_token_file: Path | None = None
    mcp_deployment_id: str = Field(default="default", min_length=1)
    mcp_json_response: bool = True
    mcp_server_sent_events: bool = False
    mcp_max_request_body_size: int = Field(default=4 * 1024 * 1024, gt=0)
    database_path: Path = Path("data/domoai.sqlite3")
    audit_database_path: Path | None = None
    policy_config_path: Path | None = None
    risk_overrides_path: Path | None = None
    safety_limits_path: Path | None = None
    home_assistant_url: str | None = None
    home_assistant_token: SecretStr | None = None
    operator_approval_token: SecretStr | None = None
    allow_legacy_operator_token: bool = False
    home_assistant_mapping_path: Path | None = None
    zigbee2mqtt_url: str | None = None
    zigbee2mqtt_base_topic: str = Field(default="zigbee2mqtt", min_length=1)
    matter_server_url: str | None = None
    knx_gateway_host: str | None = None
    knx_gateway_port: int = Field(default=3671, ge=1, le=65535)
    knx_virtual_host: str | None = None
    knx_gateway_route_back: bool = False
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
    state_refresh_interval_seconds: float = Field(default=60.0, gt=0)
    inventory_refresh_interval_seconds: float = Field(default=300.0, gt=0)
    matter_optional_node_ids: tuple[int, ...] = Field(default=(), max_length=64)
    scheduler_poll_interval_seconds: int = Field(default=30, gt=0)
    scheduler_grace_window_seconds: int = Field(default=900, gt=0)
    optimization_max_solver_time_seconds: float = Field(default=30.0, gt=0)
    optimization_max_horizon_slots: int = Field(default=10080, gt=0)
    optimization_worker_queue_capacity: int = Field(default=2, ge=0)
    optimization_worker_concurrency: int = Field(default=1, gt=0)
    optimization_worker_queue_wait_seconds: float = Field(default=0.25, gt=0)
    provider_worker_timeout_seconds: float = Field(default=10.0, gt=0)
    composite_event_queue_max_size: int = Field(default=1000, gt=0)
    sqlite_busy_timeout_ms: int = Field(default=5000, gt=0)
    sqlite_worker_queue_capacity: int = Field(default=128, ge=1)
    sqlite_worker_queue_wait_seconds: float = Field(default=0.25, gt=0)
    sqlite_operation_timeout_seconds: float = Field(default=5.0, gt=0)
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
    battery_dispatch_profile_path: Path | None = None
    ev_charging_binding_paths: tuple[Path, ...] = ()
    battery_hil_evidence_path: Path | None = None
    battery_dispatch_production: bool = False
    battery_hil_power_ceiling_kw: float | None = Field(default=None, gt=0)
    solar_timeout_seconds: float = Field(default=10.0, gt=0)
    energy_max_age_seconds: float | None = Field(default=900.0, ge=0)

    @model_validator(mode="after")
    def validate_source_selection(self) -> Settings:
        self._validate_mcp_configuration()
        self._validate_storage_isolation()
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
        if len(set(self.matter_optional_node_ids)) != len(self.matter_optional_node_ids):
            raise ValueError("DOMOAI_MATTER_OPTIONAL_NODE_IDS must be unique")
        if any(node_id < 0 for node_id in self.matter_optional_node_ids):
            raise ValueError("DOMOAI_MATTER_OPTIONAL_NODE_IDS must be non-negative")
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
        # Spec 161: the built-in-provider-only requirements (omie/open_meteo
        # provider selection, Open-Meteo's required solar fields) moved to
        # runtime_factory._create_energy_context_provider, which only runs
        # on the no-override path. Settings is constructed before
        # build_runtime is called, so it cannot know whether a caller will
        # later supply an external EnergyContextProvider -- enforcing the
        # built-in-only requirements here would incorrectly block that path.
        return self

    def _validate_storage_isolation(self) -> None:
        if self.audit_database_path is None:
            return
        if self.audit_database_path.resolve() == self.database_path.resolve():
            raise ValueError(
                "audit database must use a different file from the authority database"
            )

    def _validate_mcp_configuration(self) -> None:
        if not self.mcp_path.startswith("/") or "?" in self.mcp_path or "#" in self.mcp_path:
            raise ValueError("MCP path must be an absolute URL path without query or fragment")
        parsed_url = urlparse(self.mcp_public_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("MCP public URL must be an absolute HTTP(S) URL")
        if not _is_loopback_bind(self.mcp_host):
            if self.mcp_client_token_file is None:
                raise ValueError("non-local MCP gateway requires a client token file")
            if parsed_url.scheme != "https":
                raise ValueError("non-local MCP gateway requires an HTTPS public URL")

    @classmethod
    def from_environment(cls) -> Settings:
        stale_after = os.getenv("DOMOAI_STATE_STALE_AFTER_SECONDS", "300")
        bootstrap_profile = os.getenv("DOMOAI_BOOTSTRAP_PROFILE", "none")
        if bootstrap_profile not in {"none", "lab"}:
            raise ValueError("DOMOAI_BOOTSTRAP_PROFILE must be 'none' or 'lab'")

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
            bootstrap_profile=cast(Literal["none", "lab"], bootstrap_profile),
            bootstrap_manifest_path=(
                Path(manifest_path)
                if (manifest_path := os.getenv("DOMOAI_BOOTSTRAP_MANIFEST_PATH"))
                else None
            ),
            commissioning_manifest_path=(
                Path(manifest_path)
                if (manifest_path := os.getenv("DOMOAI_COMMISSIONING_MANIFEST_PATH"))
                else None
            ),
            mcp_host=os.getenv("DOMOAI_MCP_HOST", "127.0.0.1"),
            mcp_port=int(os.getenv("DOMOAI_MCP_PORT", "8000")),
            mcp_path=os.getenv("DOMOAI_MCP_PATH", "/mcp"),
            mcp_public_url=os.getenv(
                "DOMOAI_MCP_PUBLIC_URL", "http://127.0.0.1:8000"
            ),
            mcp_client_token_file=(
                Path(token_file)
                if (token_file := os.getenv("DOMOAI_MCP_CLIENT_TOKEN_FILE"))
                else None
            ),
            mcp_deployment_id=os.getenv("DOMOAI_MCP_DEPLOYMENT_ID", "default"),
            mcp_json_response=boolean("DOMOAI_MCP_JSON_RESPONSE", True),
            mcp_server_sent_events=boolean("DOMOAI_MCP_SERVER_SENT_EVENTS", False),
            mcp_max_request_body_size=int(
                os.getenv("DOMOAI_MCP_MAX_REQUEST_BODY_SIZE", str(4 * 1024 * 1024))
            ),
            database_path=Path(os.getenv("DOMOAI_DATABASE_PATH", "data/domoai.sqlite3")),
            audit_database_path=(
                Path(audit_path)
                if (audit_path := os.getenv("DOMOAI_AUDIT_DATABASE_PATH"))
                else None
            ),
            policy_config_path=(
                Path(policy_path)
                if (policy_path := os.getenv("DOMOAI_POLICY_CONFIG_PATH"))
                else None
            ),
            risk_overrides_path=(
                Path(risk_path) if (risk_path := os.getenv("DOMOAI_RISK_OVERRIDES_PATH")) else None
            ),
            safety_limits_path=(
                Path(safety_path)
                if (safety_path := os.getenv("DOMOAI_SAFETY_LIMITS_PATH"))
                else None
            ),
            home_assistant_url=os.getenv("DOMOAI_HOME_ASSISTANT_URL"),
            home_assistant_token=(
                SecretStr(token) if (token := os.getenv("DOMOAI_HOME_ASSISTANT_TOKEN")) else None
            ),
            operator_approval_token=(
                SecretStr(token) if (token := os.getenv("DOMOAI_OPERATOR_APPROVAL_TOKEN")) else None
            ),
            allow_legacy_operator_token=boolean("DOMOAI_ALLOW_LEGACY_OPERATOR_TOKEN"),
            home_assistant_mapping_path=(
                Path(mapping_path)
                if (mapping_path := os.getenv("DOMOAI_HOME_ASSISTANT_MAPPING_PATH"))
                else None
            ),
            zigbee2mqtt_url=os.getenv("DOMOAI_ZIGBEE2MQTT_URL"),
            zigbee2mqtt_base_topic=os.getenv("DOMOAI_ZIGBEE2MQTT_BASE_TOPIC", "zigbee2mqtt"),
            matter_server_url=os.getenv("DOMOAI_MATTER_SERVER_URL"),
            knx_gateway_host=os.getenv("DOMOAI_KNX_GATEWAY_HOST"),
            knx_gateway_port=int(os.getenv("DOMOAI_KNX_GATEWAY_PORT", "3671")),
            knx_virtual_host=os.getenv("DOMOAI_KNX_KV_HOST"),
            knx_gateway_route_back=boolean("DOMOAI_KNX_GATEWAY_ROUTE_BACK"),
            knx_config_path=(
                Path(config_path) if (config_path := os.getenv("DOMOAI_KNX_CONFIG_PATH")) else None
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
            scheduler_poll_interval_seconds=int(
                os.getenv("DOMOAI_SCHEDULER_POLL_INTERVAL_SECONDS", "30")
            ),
            scheduler_grace_window_seconds=int(
                os.getenv("DOMOAI_SCHEDULER_GRACE_WINDOW_SECONDS", "900")
            ),
            optimization_max_solver_time_seconds=float(
                os.getenv("DOMOAI_OPTIMIZATION_MAX_SOLVER_TIME_SECONDS", "30")
            ),
            optimization_max_horizon_slots=int(
                os.getenv("DOMOAI_OPTIMIZATION_MAX_HORIZON_SLOTS", "10080")
            ),
            optimization_worker_queue_capacity=int(
                os.getenv("DOMOAI_OPTIMIZATION_WORKER_QUEUE_CAPACITY", "2")
            ),
            optimization_worker_concurrency=int(
                os.getenv("DOMOAI_OPTIMIZATION_WORKER_CONCURRENCY", "1")
            ),
            optimization_worker_queue_wait_seconds=float(
                os.getenv("DOMOAI_OPTIMIZATION_WORKER_QUEUE_WAIT_SECONDS", "0.25")
            ),
            provider_worker_timeout_seconds=float(
                os.getenv("DOMOAI_PROVIDER_WORKER_TIMEOUT_SECONDS", "10")
            ),
            mqtt_username=os.getenv("DOMOAI_MQTT_USERNAME"),
            mqtt_password=(
                SecretStr(password) if (password := os.getenv("DOMOAI_MQTT_PASSWORD")) else None
            ),
            mqtt_timeout_seconds=float(os.getenv("DOMOAI_MQTT_TIMEOUT_SECONDS", "5")),
            mqtt_ca_cert_path=(
                Path(ca_path) if (ca_path := os.getenv("DOMOAI_MQTT_CA_CERT_PATH")) else None
            ),
            mqtt_client_cert_path=(
                Path(cert_path)
                if (cert_path := os.getenv("DOMOAI_MQTT_CLIENT_CERT_PATH"))
                else None
            ),
            mqtt_client_key_path=(
                Path(key_path) if (key_path := os.getenv("DOMOAI_MQTT_CLIENT_KEY_PATH")) else None
            ),
            mqtt_tls_insecure=boolean("DOMOAI_MQTT_TLS_INSECURE"),
            composite_event_queue_max_size=int(
                os.getenv("DOMOAI_COMPOSITE_EVENT_QUEUE_MAX_SIZE", "1000")
            ),
            sqlite_busy_timeout_ms=int(os.getenv("DOMOAI_SQLITE_BUSY_TIMEOUT_MS", "5000")),
            sqlite_worker_queue_capacity=int(
                os.getenv("DOMOAI_SQLITE_WORKER_QUEUE_CAPACITY", "128")
            ),
            sqlite_worker_queue_wait_seconds=float(
                os.getenv("DOMOAI_SQLITE_WORKER_QUEUE_WAIT_SECONDS", "0.25")
            ),
            sqlite_operation_timeout_seconds=float(
                os.getenv("DOMOAI_SQLITE_OPERATION_TIMEOUT_SECONDS", "5")
            ),
            matter_timeout_seconds=float(os.getenv("DOMOAI_MATTER_TIMEOUT_SECONDS", "5")),
            state_stale_after_seconds=int(stale_after),
            state_refresh_interval_seconds=float(
                os.getenv("DOMOAI_STATE_REFRESH_INTERVAL_SECONDS", "60")
            ),
            inventory_refresh_interval_seconds=float(
                os.getenv("DOMOAI_INVENTORY_REFRESH_INTERVAL_SECONDS", "300")
            ),
            matter_optional_node_ids=tuple(
                int(entry.strip())
                for entry in os.getenv("DOMOAI_MATTER_OPTIONAL_NODE_IDS", "").split(",")
                if entry.strip()
            ),
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
            battery_dispatch_profile_path=(
                Path(profile_path)
                if (profile_path := os.getenv("DOMOAI_BATTERY_DISPATCH_PROFILE_PATH"))
                else None
            ),
            ev_charging_binding_paths=tuple(
                Path(entry.strip())
                for entry in os.getenv("DOMOAI_EV_CHARGING_BINDING_PATHS", "").split(",")
                if entry.strip()
            ),
            battery_hil_evidence_path=(
                Path(evidence_path)
                if (evidence_path := os.getenv("DOMOAI_BATTERY_HIL_EVIDENCE_PATH"))
                else None
            ),
            battery_dispatch_production=boolean("DOMOAI_BATTERY_DISPATCH_PRODUCTION"),
            battery_hil_power_ceiling_kw=optional_float("DOMOAI_BATTERY_HIL_POWER_CEILING_KW"),
            solar_timeout_seconds=float(os.getenv("DOMOAI_SOLAR_TIMEOUT_SECONDS", "10")),
            energy_max_age_seconds=optional_float("DOMOAI_ENERGY_MAX_AGE_SECONDS")
            if os.getenv("DOMOAI_ENERGY_MAX_AGE_SECONDS") is not None
            else 900.0,
        )


def _is_loopback_bind(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
