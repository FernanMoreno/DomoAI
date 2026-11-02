from __future__ import annotations

import pytest

from domoai.adapters.mqtt.recipe_adapter import MqttRecipeAdapter
from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import Command, SourceRef
from domoai.runtime.mqtt_transport import InMemoryMqttTransport, MqttMessage


def _recipe() -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "acme_mqtt",
            "display_name": "Acme MQTT",
            "device_type": "light",
            "transport_family": "mqtt",
            "discovery_strategies": ["manual"],
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


def _candidate() -> ConnectionCandidate:
    return ConnectionCandidate(
        connector_id="acme_mqtt",
        candidate_id="lamp-1",
        display_name="Acme Lamp",
        endpoint={"host": "broker.local", "port": 1883},
    )


@pytest.mark.asyncio
async def test_recipe_adapter_maps_inventory_state_command_and_readback() -> None:
    transport = InMemoryMqttTransport([MqttMessage("acme/lamp-1/state", b"true")])
    adapter = MqttRecipeAdapter(transport, _recipe(), _candidate(), adapter_id="acme-lamp-1")

    await adapter.connect()
    snapshot = await adapter.discover()
    states = await adapter.read_state([SourceRef(adapter_id="acme-lamp-1", external_id="lamp-1")])

    assert snapshot.source_entities[0]["entity_id"] == "lamp-1"
    assert snapshot.source_entities[0]["domain"] == "light"
    assert states[0].value is True

    transport.enqueue(MqttMessage("acme/lamp-1/state", b"false"))
    ack = await adapter.execute(
        Command(
            id="command-1",
            device_id="lamp-1",
            command="power",
            value=False,
            idempotency_key="idempotency-1",
        )
    )

    assert ack.accepted is True
    assert transport.published[0].topic == "acme/lamp-1/set"
    assert transport.published[0].payload == b"false"


@pytest.mark.asyncio
async def test_recipe_adapter_rejects_unmapped_command() -> None:
    adapter = MqttRecipeAdapter(InMemoryMqttTransport(), _recipe(), _candidate(), adapter_id="a")
    await adapter.connect()

    with pytest.raises(ValueError, match="not declared"):
        await adapter.execute(
            Command(
                id="command-1",
                device_id="lamp-1",
                command="turn_on_unknown",
                value=True,
                idempotency_key="idempotency-1",
            )
        )
