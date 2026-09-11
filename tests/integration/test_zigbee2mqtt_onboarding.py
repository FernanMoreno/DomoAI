from __future__ import annotations

from pathlib import Path

import pytest

from domoai.adapters.zigbee2mqtt.connection import (
    BrokerTransportConfig,
    Zigbee2MqttConnectionProvider,
)
from domoai.application.connection_manager import ConnectionManager
from domoai.application.connection_providers import ConnectionProviderRegistry
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.connections import ConnectionDraft
from domoai.domain.coordination import LeaseScope
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.runtime.mqtt_transport import InMemoryMqttTransport
from domoai.security.secrets import FileSecretStore
from tests.fixtures.zigbee2mqtt import retained_messages


def _setup(provider: Zigbee2MqttConnectionProvider, url: str) -> ConnectionSetup:
    candidate = provider.manual_candidate(url)
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="zigbee2mqtt",
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options={"username": "operator"},
        ),
    )


@pytest.mark.asyncio
async def test_manager_persists_secret_and_activates_two_isolated_zigbee2mqtt_profiles(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "connections.sqlite3")
    await database.initialize()
    runtime = ManagedAdapterSet()
    secret_store = FileSecretStore(tmp_path / "master.key")
    configs: list[BrokerTransportConfig] = []

    def factory(config: BrokerTransportConfig) -> InMemoryMqttTransport:
        configs.append(config)
        return InMemoryMqttTransport(retained_messages())

    provider = Zigbee2MqttConnectionProvider(
        secret_resolver=secret_store,
        transport_factory=factory,
        timeout_seconds=0.02,
    )
    providers = ConnectionProviderRegistry()
    providers.register(provider)
    manager = ConnectionManager(
        repository=ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        ),
        providers=providers,
        runtime=runtime,
        secret_store=secret_store,
    )
    try:
        first = await manager.create(
            _setup(provider, "mqtt://broker-one.local"),
            credential_value="first-password",
        )
        second = await manager.create(
            _setup(provider, "mqtt://broker-two.local"),
            credential_value="second-password",
        )

        assert first.enabled is True
        assert second.enabled is True
        assert first.credential_ref is not None
        assert second.credential_ref is not None
        assert secret_store.resolve(first.credential_ref) == "first-password"
        assert secret_store.resolve(second.credential_ref) == "second-password"
        assert {adapter.adapter_id for adapter in runtime.adapters} == {
            f"connection:{first.id}",
            f"connection:{second.id}",
        }
        assert {config.password for config in configs} == {"first-password", "second-password"}
        assert "first-password" not in repr(first)
        assert "second-password" not in repr(second)
    finally:
        await runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_control_plane_runtime_registers_zigbee2mqtt_provider(tmp_path: Path) -> None:
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "runtime.sqlite3",
            connection_master_key_path=tmp_path / "master.key",
        ),
        control_plane_mode=True,
    )
    try:
        assert runtime.connection_provider_registry is not None
        provider = runtime.connection_provider_registry.get("zigbee2mqtt")
        assert provider is not None
        assert provider.manifest.connector_id == "zigbee2mqtt"
    finally:
        await runtime.close()
