from __future__ import annotations

import pytest

from domoai.adapters.mqtt.recipe_adapter import MqttRecipeTransport
from domoai.application.connector_recipes import RecipeConnectionProvider
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.mqtt_transport import InMemoryMqttTransport


class _DiscoveryEngine:
    async def discover(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup
    ) -> list[ConnectionCandidate]:
        return []


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "acme_mqtt",
            "display_name": "Acme MQTT",
            "transport_family": "mqtt",
            "discovery_strategies": ["manual"],
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "state_topic": "acme/{device_id}/state",
                }
            ],
        }
    )


def _setup() -> ConnectionSetup:
    candidate = ConnectionCandidate(
        connector_id="acme_mqtt",
        candidate_id="lamp-1",
        display_name="Acme Lamp",
        endpoint={"host": "broker.local", "port": 1883},
    )
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="acme_mqtt",
            unique_id="lamp-1",
            endpoint=candidate.endpoint,
            options={},
        ),
    )


@pytest.mark.asyncio
async def test_recipe_provider_implements_five_methods_and_builds_transport_adapter() -> None:
    recipe = _recipe()
    provider = RecipeConnectionProvider(
        ConnectionProviderManifest(
            connector_id="acme_mqtt",
            display_name="Acme MQTT",
            protocol="mqtt",
            version="1.0",
            recipe=recipe,
        ),
        discovery_engines={"manual": _DiscoveryEngine()},
        transport_engines={"mqtt": MqttRecipeTransport(InMemoryMqttTransport())},
        allowed_hosts=frozenset({"broker.local"}),
        allowed_ports=frozenset({1883}),
    )

    validation = await provider.validate(_setup())
    adapter = await provider.build_adapter(_setup(), adapter_id="acme-lamp-1")

    assert validation.valid is True
    assert adapter.adapter_id == "acme-lamp-1"
    assert await provider.diagnose(_setup()) == ()


def test_recipe_contract_preserves_safe_bearer_descriptions_and_templates() -> None:
    recipe = ConnectorRecipe.model_validate(
        {
            "connector_id": "acme_mqtt",
            "display_name": "Bearer protocol",
            "transport_family": "mqtt",
            "discovery_options": {"description": "Bearer protocol"},
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "state_topic": "acme/{device_id}/state",
                    "command": {
                        "name": "power",
                        "command_topic": "acme/{device_id}/set",
                        "payload_template": "{value}",
                        "readback_topic": "acme/{device_id}/state",
                    },
                }
            ],
        }
    )

    assert recipe.display_name == "Bearer protocol"
    assert recipe.discovery_options["description"] == "Bearer protocol"
    assert recipe.capabilities[0].command is not None
    assert recipe.capabilities[0].command.payload_template == "{value}"
