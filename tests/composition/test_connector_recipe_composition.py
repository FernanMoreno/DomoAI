from __future__ import annotations

from pathlib import Path

import pytest

from domoai.adapters.mqtt.recipe_adapter import MqttRecipeTransport
from domoai.application.connection_manager import ConnectionManager
from domoai.application.connection_providers import ConnectionProviderRegistry
from domoai.application.connector_recipes import RecipeConnectionProvider
from domoai.application.discovery_service import DiscoveryService
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.coordination import LeaseScope
from domoai.domain.models import Command
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.events import AuditLog
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.runtime.mqtt_transport import InMemoryMqttTransport, MqttMessage
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


class _ManualDiscovery:
    async def discover(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup
    ) -> list[ConnectionCandidate]:
        return []


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "composition_mqtt",
            "display_name": "Composition MQTT",
            "device_type": "light",
            "transport_family": "mqtt",
            "discovery_strategies": ["manual"],
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "state_topic": "composition/{device_id}/state",
                    "command": {
                        "name": "power",
                        "command_topic": "composition/{device_id}/set",
                        "payload_template": "{value}",
                        "readback_topic": "composition/{device_id}/state",
                    },
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_recipe_flows_through_manager_runtime_registry_and_detach(tmp_path: Path) -> None:
    transport = InMemoryMqttTransport([MqttMessage("composition/lamp-1/state", b"true")])
    recipe = _recipe()
    candidate = ConnectionCandidate(
        connector_id=recipe.connector_id,
        candidate_id="lamp-1",
        display_name="Composition lamp",
        endpoint={"host": "broker.local", "port": 1883},
    )
    setup = ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id=recipe.connector_id,
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options={},
        ),
    )
    provider = RecipeConnectionProvider(
        ConnectionProviderManifest(
            connector_id=recipe.connector_id,
            display_name=recipe.display_name,
            protocol="mqtt",
            version="1.0",
            recipe=recipe,
        ),
        discovery_engines={"manual": _ManualDiscovery()},
        transport_engines={"mqtt": MqttRecipeTransport(transport)},
        allowed_hosts=frozenset({"broker.local"}),
        allowed_ports=frozenset({1883}),
    )
    providers = ConnectionProviderRegistry()
    providers.register(provider)
    registry = DeviceRegistry()
    runtime = ManagedAdapterSet(registry=registry)
    state_store = StateStore()
    discovery = DiscoveryService(runtime, registry, state_store, AuditLog())
    database = SQLiteDatabase(tmp_path / "connections.sqlite3")
    await database.initialize()
    manager = ConnectionManager(
        repository=ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        ),
        providers=providers,
        runtime=runtime,
        refresh_inventory=discovery.refresh,
    )

    try:
        profile = await manager.create(setup)
        assert profile.enabled is True
        assert len(runtime.adapters) == 1

        device = registry.devices[0]
        assert device.id == "unassigned.lamp-1"
        assert device.type.value == "light"
        assert device.capabilities[0].commands == ["power"]
        assert state_store.peek(device.id, "power") is not None

        transport.enqueue(MqttMessage("composition/lamp-1/state", b"false"))
        ack = await runtime.execute(
            Command(
                id="composition-command",
                device_id=device.id,
                command="power",
                value=False,
                idempotency_key="composition-idempotency",
            )
        )
        assert ack.accepted is True
        assert transport.published[0].topic == "composition/lamp-1/set"

        assert await manager.disable(profile.id) is True
        assert runtime.adapters == ()
    finally:
        await runtime.disconnect()
        await database.close()
