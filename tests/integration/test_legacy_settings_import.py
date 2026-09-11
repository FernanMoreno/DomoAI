from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.application.connection_manager import ConnectionManager
from domoai.application.connection_providers import ConnectionProviderRegistry
from domoai.application.legacy_settings_importer import LegacySettingsImporter
from domoai.config.settings import Settings
from domoai.domain.coordination import LeaseScope
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.security.secrets import FileSecretStore


def test_importer_detects_all_supported_legacy_families_without_plaintext(
    tmp_path: Path,
) -> None:
    settings = Settings(
        home_assistant_url="http://ha.local",
        home_assistant_token=SecretStr("legacy-ha-token"),
        home_assistant_mapping_path=tmp_path / "ha.json",
        knx_gateway_host="knx.local",
        knx_config_path=tmp_path / "knx.json",
        matter_server_url="ws://matter.local",
        zigbee2mqtt_url="mqtt://zigbee.local",
        generic_mqtt_url="mqtt://mqtt.local",
        generic_mqtt_mapping_path=tmp_path / "mqtt.json",
        modbus_host="modbus.local",
        modbus_config_path=tmp_path / "modbus.json",
    )
    store = FileSecretStore(tmp_path / "master.key")
    report = LegacySettingsImporter(secret_store=store).import_settings(
        settings,
        environment={"DOMOAI_HOME_ASSISTANT_TOKEN": "legacy-ha-token"},
    )

    assert set(report.detected_connectors) == {
        "home_assistant",
        "knx",
        "matter",
        "zigbee2mqtt",
        "mqtt",
        "modbus",
    }
    assert len(report.profiles) == 6
    assert report.profiles[0].enabled is False
    assert all(profile.credential_ref != "legacy-ha-token" for profile in report.profiles)
    assert "legacy-ha-token" not in repr(report)
    assert any(
        profile.credential_ref == "env:DOMOAI_HOME_ASSISTANT_TOKEN" for profile in report.profiles
    )


def test_importer_reports_partial_legacy_configuration_and_leaves_it_for_manual_setup() -> None:
    report = LegacySettingsImporter().import_settings(
        Settings(home_assistant_url="http://ha.local")
    )

    assert report.profiles == []
    assert any(warning.code == "incomplete_configuration" for warning in report.warnings)


@pytest.mark.asyncio
async def test_manager_import_is_explicit_persisted_and_disabled(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "connections.sqlite3")
    await database.initialize()
    try:
        settings = Settings(
            home_assistant_url="http://ha.local",
            home_assistant_token=SecretStr("legacy-token"),
        )
        manager = ConnectionManager(
            repository=ConnectionRepository(
                database,
                scope=LeaseScope(
                    tenant_id="default", household_id="default", deployment_id="default"
                ),
            ),
            providers=ConnectionProviderRegistry(),
            runtime=ManagedAdapterSet(),
            secret_store=FileSecretStore(tmp_path / "master.key"),
            legacy_settings=settings,
        )

        report = await manager.import_legacy(
            environment={"DOMOAI_HOME_ASSISTANT_TOKEN": "legacy-token"}
        )
        profiles = await manager.list()

        assert len(report.profiles) == 1
        assert len(profiles) == 1
        assert profiles[0].enabled is False
        assert profiles[0].credential_ref == "env:DOMOAI_HOME_ASSISTANT_TOKEN"
        assert await manager.delete(profiles[0].id) is True
    finally:
        await database.close()
