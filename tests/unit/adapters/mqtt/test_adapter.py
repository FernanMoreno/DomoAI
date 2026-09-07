from datetime import UTC, datetime

import pytest

from domoai.adapters.mqtt.adapter import GenericMqttAdapter
from domoai.adapters.mqtt.config import MqttAdapterMapping
from domoai.adapters.zigbee2mqtt.transport import InMemoryMqttTransport, MqttMessage
from domoai.domain.models import Command, SourceRef, StateStatus
from domoai.runtime.clock import FixedClock


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_discovers_declared_state_from_its_topic() -> None:
    adapter = GenericMqttAdapter(
        InMemoryMqttTransport([MqttMessage(topic="home/lamp/state", payload=b"true")]),
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [{"name": "power", "state_topic": "home/lamp/state"}],
                    }
                ],
            }
        ),
        clock=FixedClock(datetime(2026, 9, 6, tzinfo=UTC)),
    )

    await adapter.connect()
    snapshot = await adapter.discover()

    assert snapshot.source_states == [
        {
            "entity_id": "esp.lamp",
            "capability": "power",
            "value": True,
            "unit": None,
            "available": True,
            "observed_at": datetime(2026, 9, 6, tzinfo=UTC),
        }
    ]


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_returns_canonical_state_snapshots() -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    adapter = GenericMqttAdapter(
        InMemoryMqttTransport([MqttMessage(topic="home/lamp/state", payload=b"true")]),
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [{"name": "power", "state_topic": "home/lamp/state"}],
                    }
                ],
            }
        ),
        clock=FixedClock(now),
    )
    await adapter.connect()
    await adapter.discover()

    states = await adapter.read_state([SourceRef(adapter_id="mqtt", external_id="esp.lamp")])

    assert states[0].status is StateStatus.CURRENT
    assert states[0].source_ref.external_id == "esp.lamp"
    assert states[0].value is True


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_preserves_declared_state_unit() -> None:
    adapter = GenericMqttAdapter(
        InMemoryMqttTransport([MqttMessage(topic="home/thermostat/temperature", payload=b"21.5")]),
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.thermostat",
                        "type": "climate",
                        "capabilities": [
                            {
                                "name": "temperature",
                                "kind": "number",
                                "unit": "°C",
                                "state_topic": "home/thermostat/temperature",
                            }
                        ],
                    }
                ],
            }
        ),
    )
    await adapter.connect()
    await adapter.discover()

    states = await adapter.read_state(
        [SourceRef(adapter_id="mqtt", external_id="esp.thermostat")]
    )

    assert states[0].unit == "°C"


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_publishes_declared_command_once() -> None:
    transport = InMemoryMqttTransport()
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {
                                "name": "power",
                                "state_topic": "home/lamp/state",
                                "command_topic": "home/lamp/set",
                            }
                        ],
                    }
                ],
            }
        ),
    )
    await adapter.connect()

    ack = await adapter.execute(
        Command(
            id="mqtt-command",
            device_id="esp.lamp",
            command="power",
            value=True,
            idempotency_key="mqtt-key",
        )
    )

    assert ack.accepted is True
    assert transport.published[0].topic == "home/lamp/set"
    assert transport.published[0].payload == b"true"


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_accepts_declared_semantic_command_alias() -> None:
    transport = InMemoryMqttTransport()
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.thermostat",
                        "type": "climate",
                        "capabilities": [
                            {
                                "name": "target_temperature",
                                "kind": "number",
                                "unit": "°C",
                                "minimum": 16,
                                "maximum": 30,
                                "state_topic": "home/thermostat/target",
                                "command_topic": "home/thermostat/target/set",
                                "commands": ["set_temperature"],
                            }
                        ],
                    }
                ],
            }
        ),
    )
    await adapter.connect()

    ack = await adapter.execute(
        Command(
            id="temperature-command",
            device_id="esp.thermostat",
            command="set_temperature",
            value=21,
            unit="°C",
            idempotency_key="temperature-key",
        )
    )

    assert ack.accepted is True
    assert transport.published[0].topic == "home/thermostat/target/set"


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_emits_only_declared_state_events() -> None:
    transport = InMemoryMqttTransport()
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [{"name": "power", "state_topic": "home/lamp/state"}],
                    }
                ],
            }
        ),
    )
    await adapter.connect()
    events = adapter.subscribe_events()
    transport.enqueue(MqttMessage(topic="home/unknown/state", payload=b"true"))
    transport.enqueue(MqttMessage(topic="home/lamp/state", payload=b"false"))

    event = await events.__anext__()

    assert event.kind == "state_changed"
    assert event.external_id == "esp.lamp"
    assert event.capability == "power"
    await events.aclose()


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_suppresses_duplicate_idempotency_key() -> None:
    transport = InMemoryMqttTransport()
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {
                                "name": "power",
                                "state_topic": "home/lamp/state",
                                "command_topic": "home/lamp/set",
                            }
                        ],
                    }
                ],
            }
        ),
    )
    await adapter.connect()
    command = Command(
        id="mqtt-command",
        device_id="esp.lamp",
        command="power",
        value=True,
        idempotency_key="same-key",
    )

    await adapter.execute(command)
    duplicate = await adapter.execute(command)

    assert duplicate.accepted is True
    assert len(transport.published) == 1


@pytest.mark.asyncio
async def test_generic_mqtt_required_readback_must_match_command_value() -> None:
    transport = InMemoryMqttTransport()
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {
                                "name": "power",
                                "state_topic": "home/lamp/state",
                                "command_topic": "home/lamp/set",
                                "feedback_topic": "home/lamp/state",
                                "require_readback": True,
                            }
                        ],
                    }
                ],
            }
        ),
    )
    await adapter.connect()

    ack = await adapter.execute(
        Command(
            id="readback",
            device_id="esp.lamp",
            command="power",
            value=True,
            idempotency_key="readback-key",
        )
    )

    assert ack.accepted is False


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_drops_malformed_payload_without_state() -> None:
    transport = InMemoryMqttTransport([MqttMessage(topic="home/lamp/state", payload=b"{not-json")])
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [{"name": "power", "state_topic": "home/lamp/state"}],
                    }
                ],
            }
        ),
    )
    await adapter.connect()

    snapshot = await adapter.discover()

    assert snapshot.source_states == []


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_drops_typed_payload_outside_declared_range() -> None:
    transport = InMemoryMqttTransport(
        [MqttMessage(topic="home/thermostat/target", payload=b"31")]
    )
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.thermostat",
                        "type": "climate",
                        "capabilities": [
                            {
                                "name": "target_temperature",
                                "kind": "number",
                                "minimum": 16,
                                "maximum": 30,
                                "state_topic": "home/thermostat/target",
                            }
                        ],
                    }
                ],
            }
        ),
    )
    await adapter.connect()

    snapshot = await adapter.discover()

    assert snapshot.source_states == []


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_rejects_command_outside_declared_range_before_publish() -> None:
    transport = InMemoryMqttTransport()
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.thermostat",
                        "type": "climate",
                        "capabilities": [
                            {
                                "name": "target_temperature",
                                "kind": "number",
                                "minimum": 16,
                                "maximum": 30,
                                "state_topic": "home/thermostat/target",
                                "command_topic": "home/thermostat/target/set",
                            }
                        ],
                    }
                ],
            }
        ),
    )
    await adapter.connect()

    with pytest.raises(ValueError, match="maximum"):
        await adapter.execute(
            Command(
                id="temperature-command",
                device_id="esp.thermostat",
                command="target_temperature",
                value=31,
                idempotency_key="temperature-key",
            )
        )

    assert transport.published == []


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_rejects_command_with_wrong_unit_before_publish() -> None:
    transport = InMemoryMqttTransport()
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.thermostat",
                        "type": "climate",
                        "capabilities": [
                            {
                                "name": "target_temperature",
                                "kind": "number",
                                "unit": "°C",
                                "minimum": 16,
                                "maximum": 30,
                                "state_topic": "home/thermostat/target",
                                "command_topic": "home/thermostat/target/set",
                            }
                        ],
                    }
                ],
            }
        ),
    )
    await adapter.connect()

    with pytest.raises(ValueError, match="unit"):
        await adapter.execute(
            Command(
                id="temperature-command",
                device_id="esp.thermostat",
                command="target_temperature",
                value=21,
                unit="°F",
                idempotency_key="temperature-unit-key",
            )
        )

    assert transport.published == []


@pytest.mark.asyncio
async def test_generic_mqtt_adapter_can_reconnect_after_disconnect() -> None:
    transport = InMemoryMqttTransport()
    adapter = GenericMqttAdapter(
        transport,
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [{"name": "power", "state_topic": "home/lamp/state"}],
                    }
                ],
            }
        ),
    )

    await adapter.connect()
    await adapter.disconnect()
    await adapter.connect()

    assert (await adapter.health()).connected is True
