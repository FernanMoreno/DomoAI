from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from domoai.adapters.home_assistant.connection import HomeAssistantConnectionProvider
from domoai.application.connection_manager import ConnectionManager
from domoai.application.connection_providers import (
    ConnectionProviderRegistry,
    ConnectionSetup,
)
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.domain.coordination import LeaseScope
from domoai.domain.models import Command
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.security.secrets import FileSecretStore


class FakeHomeAssistantClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token
        self.closed = False

    async def health(self) -> bool:
        return True

    async def fetch_entity_registry(self) -> list[dict[str, object]]:
        return []

    async def fetch_device_registry(self) -> list[dict[str, object]]:
        return []

    async def fetch_states(self) -> list[dict[str, object]]:
        return [
            {
                "entity_id": "light.kitchen",
                "state": "off",
                "attributes": {"friendly_name": "Kitchen"},
            }
        ]

    async def call_service(
        self, domain: str, service: str, data: dict[str, object], **kwargs: object
    ) -> list[dict[str, object]]:
        return []

    def subscribe_state_events(self) -> AsyncIterator[dict[str, object]]:
        async def events() -> AsyncIterator[dict[str, object]]:
            if False:
                yield {}

        return events()

    async def close(self) -> None:
        self.closed = True


def _setup() -> ConnectionSetup:
    candidate = ConnectionCandidate(
        connector_id="home_assistant",
        candidate_id="ha:house-1",
        display_name="Home Assistant",
        endpoint={"url": "http://ha.local"},
    )
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="home_assistant",
            unique_id="ha:house-1",
            endpoint={"url": "http://ha.local"},
            options={},
        ),
    )


@pytest.mark.asyncio
async def test_home_assistant_onboarding_activates_existing_runtime_adapter(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "connections.sqlite3")
    await database.initialize()
    runtime = ManagedAdapterSet()
    secret_store = FileSecretStore(tmp_path / "master.key")
    providers = ConnectionProviderRegistry()
    providers.register(
        HomeAssistantConnectionProvider(
            secret_resolver=secret_store,
            client_factory=FakeHomeAssistantClient,
        )
    )
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
        profile = await manager.create(_setup(), credential_value="ha-token")

        assert profile.enabled is True
        assert profile.credential_ref is not None
        assert profile.credential_ref.startswith("secret:")
        assert runtime.adapters[0].adapter_id == f"connection:{profile.id}"
        assert runtime.adapters[0].provider.client.token == "ha-token"
        snapshot = await runtime.adapters[0].discover()
        assert snapshot.source_entities
        acknowledgement = await runtime.adapters[0].execute_source(
            Command(
                id="ha-command",
                device_id="kitchen",
                command="turn_on",
                idempotency_key="ha-command",
            ),
            "light.kitchen",
        )
        assert acknowledgement.source_ref is not None
        assert acknowledgement.source_ref.adapter_id == f"connection:{profile.id}"
    finally:
        await runtime.disconnect()
        await database.close()
