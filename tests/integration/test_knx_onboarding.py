from __future__ import annotations

import json
from pathlib import Path

import pytest

from domoai.adapters.knx.config import KnxMappingDocument
from domoai.adapters.knx.connection import KnxConnectionProvider
from domoai.adapters.knx.transport import InMemoryKnxTransport
from domoai.application.connection_manager import ConnectionManager
from domoai.application.connection_providers import ConnectionProviderRegistry, ConnectionSetup
from domoai.domain.connections import ConnectionDraft
from domoai.domain.coordination import LeaseScope
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.managed_adapter_set import ManagedAdapterSet


def _mapping_path(tmp_path: Path) -> Path:
    mapping = KnxMappingDocument.model_validate(
        {
            "schema_version": "v1",
            "entities": [
                {
                    "entity_id": "light.kitchen",
                    "name": "Kitchen",
                    "semantic_type": "light",
                    "capabilities": [
                        {
                            "name": "power",
                            "dpt": "1.001",
                            "state_group_address": "1/0/1",
                            "command_group_address": "1/0/2",
                        }
                    ],
                }
            ],
        }
    )
    path = tmp_path / "knx.json"
    path.write_text(json.dumps(mapping.model_dump(mode="json")), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_knx_onboarding_persists_mapping_and_activates_runtime_adapter(
    tmp_path: Path,
) -> None:
    mapping_path = _mapping_path(tmp_path)
    transport = InMemoryKnxTransport()
    provider = KnxConnectionProvider(
        transport_factory=lambda host, port, route_back, group_dpts: transport
    )
    providers = ConnectionProviderRegistry()
    providers.register(provider)
    database = SQLiteDatabase(tmp_path / "connections.sqlite3")
    await database.initialize()
    runtime = ManagedAdapterSet()
    manager = ConnectionManager(
        repository=ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        ),
        providers=providers,
        runtime=runtime,
    )
    candidate = provider.manual_candidate("gateway.local")
    setup = ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="knx",
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options={},
            mapping_ref=str(mapping_path),
        ),
    )
    try:
        profile = await manager.create(setup)

        assert profile.enabled is True
        assert profile.mapping_ref == str(mapping_path)
        assert transport.connected is True
        assert runtime.adapters[0].adapter_id == f"connection:{profile.id}"
    finally:
        await runtime.disconnect()
        await database.close()
