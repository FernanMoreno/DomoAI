"""Explicit, non-destructive conversion of legacy provider settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field, SecretStr

from domoai.config.settings import Settings
from domoai.domain.connections import ConnectionProfile
from domoai.domain.models import StrictModel
from domoai.security.secrets import SecretStorePort


class LegacyImportWarning(StrictModel):
    connector_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=240)


class LegacyImportReport(StrictModel):
    profiles: list[ConnectionProfile] = Field(default_factory=list)
    detected_connectors: tuple[str, ...] = ()
    warnings: list[LegacyImportWarning] = Field(default_factory=list)


class LegacySettingsImporter:
    """Convert known settings to disabled profiles for explicit activation."""

    def __init__(self, *, secret_store: SecretStorePort | None = None) -> None:
        self.secret_store = secret_store

    def import_settings(
        self,
        settings: Settings,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> LegacyImportReport:
        env = os.environ if environment is None else environment
        profiles: list[ConnectionProfile] = []
        warnings: list[LegacyImportWarning] = []
        detected: list[str] = []

        self._import_home_assistant(settings, env, profiles, warnings, detected)
        self._import_knx(settings, profiles, warnings, detected)
        self._import_matter(settings, profiles, warnings, detected)
        self._import_zigbee(settings, env, profiles, warnings, detected)
        self._import_mqtt(settings, env, profiles, warnings, detected)
        self._import_modbus(settings, profiles, warnings, detected)

        return LegacyImportReport(
            profiles=profiles,
            detected_connectors=tuple(dict.fromkeys(detected)),
            warnings=warnings,
        )

    def _import_home_assistant(
        self,
        settings: Settings,
        env: Mapping[str, str],
        profiles: list[ConnectionProfile],
        warnings: list[LegacyImportWarning],
        detected: list[str],
    ) -> None:
        url, token = settings.home_assistant_url, settings.home_assistant_token
        if url is None and token is None:
            return
        connector = "home_assistant"
        if url is None or token is None:
            self._incomplete(connector, warnings)
            return
        detected.append(connector)
        credential_ref = self._secret_ref(
            "DOMOAI_HOME_ASSISTANT_TOKEN", token, env, connector, warnings
        )
        profiles.append(
            self._profile(
                connector,
                f"ha:{url.rstrip('/')}",
                {"url": url},
                {},
                credential_ref=credential_ref,
                mapping_ref=_path_ref(settings.home_assistant_mapping_path),
            )
        )
        self._deprecated(connector, warnings)

    def _import_knx(
        self,
        settings: Settings,
        profiles: list[ConnectionProfile],
        warnings: list[LegacyImportWarning],
        detected: list[str],
    ) -> None:
        host, mapping = settings.knx_gateway_host, settings.knx_config_path
        if host is None and mapping is None:
            return
        connector = "knx"
        if host is None or mapping is None:
            self._incomplete(connector, warnings)
            return
        detected.append(connector)
        profiles.append(
            self._profile(
                connector,
                f"knx:{host}:{settings.knx_gateway_port}",
                {"host": host, "port": settings.knx_gateway_port},
                {"route_back": settings.knx_gateway_route_back},
                mapping_ref=str(mapping),
            )
        )
        self._deprecated(connector, warnings)

    def _import_matter(
        self,
        settings: Settings,
        profiles: list[ConnectionProfile],
        warnings: list[LegacyImportWarning],
        detected: list[str],
    ) -> None:
        if settings.matter_server_url is None:
            return
        connector = "matter"
        detected.append(connector)
        profiles.append(
            self._profile(
                connector,
                f"matter:{settings.matter_server_url}",
                {"url": settings.matter_server_url},
                {},
            )
        )
        self._deprecated(connector, warnings)

    def _import_zigbee(
        self,
        settings: Settings,
        env: Mapping[str, str],
        profiles: list[ConnectionProfile],
        warnings: list[LegacyImportWarning],
        detected: list[str],
    ) -> None:
        if settings.zigbee2mqtt_url is None:
            return
        connector = "zigbee2mqtt"
        detected.append(connector)
        profiles.append(
            self._profile(
                connector,
                f"zigbee2mqtt:{settings.zigbee2mqtt_url}",
                {"url": settings.zigbee2mqtt_url},
                {"base_topic": settings.zigbee2mqtt_base_topic},
                credential_ref=self._mqtt_secret(settings, env, connector, warnings),
            )
        )
        self._deprecated(connector, warnings)

    def _import_mqtt(
        self,
        settings: Settings,
        env: Mapping[str, str],
        profiles: list[ConnectionProfile],
        warnings: list[LegacyImportWarning],
        detected: list[str],
    ) -> None:
        url, mapping = settings.generic_mqtt_url, settings.generic_mqtt_mapping_path
        if url is None and mapping is None:
            return
        connector = "mqtt"
        if url is None or mapping is None:
            self._incomplete(connector, warnings)
            return
        detected.append(connector)
        profiles.append(
            self._profile(
                connector,
                f"mqtt:{url}",
                {"url": url},
                {"username": settings.mqtt_username or ""},
                credential_ref=self._mqtt_secret(settings, env, connector, warnings),
                mapping_ref=str(mapping),
            )
        )
        self._deprecated(connector, warnings)

    def _import_modbus(
        self,
        settings: Settings,
        profiles: list[ConnectionProfile],
        warnings: list[LegacyImportWarning],
        detected: list[str],
    ) -> None:
        host, mapping = settings.modbus_host, settings.modbus_config_path
        if host is None and mapping is None:
            return
        connector = "modbus"
        if host is None or mapping is None:
            self._incomplete(connector, warnings)
            return
        detected.append(connector)
        profiles.append(
            self._profile(
                connector,
                f"modbus:{host}:{settings.modbus_port}",
                {"host": host, "port": settings.modbus_port},
                {},
                mapping_ref=str(mapping),
            )
        )
        self._deprecated(connector, warnings)

    def _mqtt_secret(
        self,
        settings: Settings,
        env: Mapping[str, str],
        connector: str,
        warnings: list[LegacyImportWarning],
    ) -> str | None:
        return self._secret_ref(
            "DOMOAI_MQTT_PASSWORD", settings.mqtt_password, env, connector, warnings
        )

    def _secret_ref(
        self,
        env_name: str,
        value: SecretStr | None,
        env: Mapping[str, str],
        connector: str,
        warnings: list[LegacyImportWarning],
    ) -> str | None:
        if value is None:
            return None
        if env_name in env:
            return f"env:{env_name}"
        if self.secret_store is not None:
            return self.secret_store.put(value.get_secret_value())
        warnings.append(
            LegacyImportWarning(
                connector_id=connector,
                code="credential_requires_manual_setup",
                message="legacy credential was not copied; complete setup manually",
            )
        )
        return None

    @staticmethod
    def _profile(
        connector_id: str,
        unique_id: str,
        endpoint: dict[str, Any],
        options: dict[str, Any],
        *,
        credential_ref: str | None = None,
        mapping_ref: str | None = None,
    ) -> ConnectionProfile:
        now = datetime.now(UTC)
        return ConnectionProfile(
            id=uuid4(),
            connector_id=connector_id,
            unique_id=unique_id,
            endpoint=endpoint,
            options=options,
            credential_ref=credential_ref,
            mapping_ref=mapping_ref,
            enabled=False,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _incomplete(connector_id: str, warnings: list[LegacyImportWarning]) -> None:
        warnings.append(
            LegacyImportWarning(
                connector_id=connector_id,
                code="incomplete_configuration",
                message="legacy configuration is incomplete; existing settings were left unchanged",
            )
        )

    @staticmethod
    def _deprecated(connector_id: str, warnings: list[LegacyImportWarning]) -> None:
        warnings.append(
            LegacyImportWarning(
                connector_id=connector_id,
                code="legacy_deprecated",
                message="legacy settings remain supported; prefer the connection control plane",
            )
        )


def _path_ref(path: Path | None) -> str | None:
    return str(path) if path is not None else None


__all__ = ["LegacyImportReport", "LegacyImportWarning", "LegacySettingsImporter"]
