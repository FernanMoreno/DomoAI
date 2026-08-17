from time import perf_counter

import pytest

from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.adapters.zigbee2mqtt.transport import InMemoryMqttTransport
from domoai.domain.models import Command
from tests.fixtures.zigbee2mqtt import retained_messages


@pytest.mark.asyncio
async def test_twenty_device_discovery_stays_under_one_second() -> None:
    adapter = Zigbee2MqttAdapter(
        InMemoryMqttTransport(retained_messages(20)), discovery_timeout=0.05
    )
    await adapter.connect()

    started = perf_counter()
    snapshot = await adapter.discover()
    elapsed = perf_counter() - started

    assert len(snapshot.source_entities) == 20
    assert elapsed < 1


@pytest.mark.asyncio
async def test_fixture_command_and_event_processing_stay_under_one_second() -> None:
    transport = InMemoryMqttTransport(retained_messages(20))
    adapter = Zigbee2MqttAdapter(transport, discovery_timeout=0.05)
    await adapter.connect()
    await adapter.discover()

    command_started = perf_counter()
    result = await adapter.execute(
        Command(
            id="performance-command",
            device_id="unassigned.living-room-main-light",
            command="turn_on",
            idempotency_key="performance-intent",
        )
    )
    command_elapsed = perf_counter() - command_started

    transport.enqueue(
        next(
            message
            for message in retained_messages()
            if message.topic == "zigbee2mqtt/living_room/main_light"
        )
    )
    event_started = perf_counter()
    event = await anext(adapter.subscribe_events())
    event_elapsed = perf_counter() - event_started

    assert result.accepted is True
    assert event.kind == "state_changed"
    assert command_elapsed < 1
    assert event_elapsed < 1
