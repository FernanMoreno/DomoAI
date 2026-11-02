from __future__ import annotations

import json

import pytest

from domoai.adapters.mqtt.recipe_discovery import MqttRecipeDiscovery
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.mqtt_transport import InMemoryMqttTransport, MqttMessage


def _recipe(**options: object) -> ConnectorRecipe:
    return ConnectorRecipe.model_validate(
        {
            "connector_id": "acme_mqtt",
            "display_name": "Acme MQTT",
            "transport_family": "mqtt",
            "discovery_strategies": ["mqtt_discovery"],
            "discovery_options": {"topic": "acme/discovery", **options},
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
        candidate_id="broker-1",
        display_name="Broker",
        endpoint={"host": "broker.local", "port": 1883},
    )
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="acme_mqtt",
            unique_id="broker-1",
            endpoint=candidate.endpoint,
            options={},
        ),
    )


@pytest.mark.asyncio
async def test_mqtt_recipe_discovery_reads_declared_exact_topic() -> None:
    transport = InMemoryMqttTransport(
        [
            MqttMessage(
                "acme/discovery",
                json.dumps(
                    {
                        "candidate_id": "acme-gateway-1",
                        "display_name": "Acme Gateway",
                        "host": "gateway.local",
                        "port": 1883,
                    }
                ).encode(),
                retained=True,
            ),
            MqttMessage("acme/other", b"{}", retained=True),
        ]
    )

    candidates = await MqttRecipeDiscovery(transport).discover(_recipe(), _setup())

    assert transport.subscriptions == ["acme/discovery"]
    assert [candidate.candidate_id for candidate in candidates] == ["acme-gateway-1"]
    assert candidates[0].endpoint == {"host": "gateway.local", "port": 1883}
    assert candidates[0].metadata == {"source": "mqtt_recipe_discovery"}


@pytest.mark.asyncio
async def test_mqtt_recipe_discovery_skips_malformed_or_oversized_payloads() -> None:
    transport = InMemoryMqttTransport(
        [MqttMessage("acme/discovery", b"not-json"), MqttMessage("acme/discovery", b"x" * 20)]
    )

    candidates = await MqttRecipeDiscovery(transport, max_payload_bytes=16).discover(
        _recipe(), _setup()
    )

    assert candidates == []


def test_mqtt_recipe_discovery_rejects_wildcard_topic() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        MqttRecipeDiscovery(InMemoryMqttTransport())._topic(_recipe(topic="acme/#"))
