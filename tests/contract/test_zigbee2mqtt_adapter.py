import pytest

from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.adapters.zigbee2mqtt.transport import (
    InMemoryMqttTransport,
    MqttMessage,
)
from domoai.domain.models import Command, SourceRef, StateStatus
from tests.fixtures.zigbee2mqtt import retained_messages, state_message


@pytest.mark.asyncio
async def test_in_memory_transport_preserves_mqtt_contract() -> None:
    transport = InMemoryMqttTransport()

    await transport.connect()
    await transport.subscribe("zigbee2mqtt/#")
    await transport.publish("zigbee2mqtt/device/set", b'{"state":"ON"}')
    transport.enqueue(MqttMessage("zigbee2mqtt/device", b'{"state":"ON"}', retained=True))
    received = await transport.receive(0.05)

    assert transport.subscriptions == ["zigbee2mqtt/#"]
    assert transport.published[0].topic == "zigbee2mqtt/device/set"
    assert received is not None
    assert received.retained is True


async def build_adapter() -> tuple[InMemoryMqttTransport, Zigbee2MqttAdapter]:
    transport = InMemoryMqttTransport(retained_messages())
    adapter = Zigbee2MqttAdapter(transport, discovery_timeout=0.05)
    await adapter.connect()
    return transport, adapter


class _IdleOncePersistentTransport(InMemoryMqttTransport):
    """Forces the first `receive()` call to time out, regardless of queue state."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._idle_returned = False

    async def receive(self, timeout: float | None = None):  # type: ignore[override]
        if not self._idle_returned:
            self._idle_returned = True
            return None
        return await super().receive(timeout)


@pytest.mark.asyncio
async def test_subscribe_events_survives_idle_poll_and_yields_next_event() -> None:
    transport = _IdleOncePersistentTransport()
    adapter = Zigbee2MqttAdapter(transport, discovery_timeout=0.05)
    await adapter.connect()

    transport.enqueue(state_message("zigbee2mqtt/living_room/main_light", {"state": "ON"}))
    event = await anext(adapter.subscribe_events())

    assert event.kind == "state_changed"


@pytest.mark.asyncio
async def test_zigbee2mqtt_discovery_returns_canonical_devices_and_capabilities() -> None:
    _transport, adapter = await build_adapter()

    snapshot = await adapter.discover()

    light = next(entity for entity in snapshot.source_entities if entity["domain"] == "light")
    assert light["device_id"].startswith("0x00158d")
    assert light["manufacturer"] == "Fixture Lamps"
    assert {item["name"] for item in light["capabilities"]} == {"power", "brightness"}
    assert light["available"] is True
    assert light["entity_id"] == "living_room/main_light"


@pytest.mark.asyncio
async def test_zigbee2mqtt_command_payloads_and_brightness_conversion_are_bounded() -> None:
    transport, adapter = await build_adapter()
    await adapter.discover()

    light_id = "unassigned.living-room-main-light"
    accepted = await adapter.execute(
        Command(
            id="command-on",
            device_id=light_id,
            command="turn_on",
            idempotency_key="intent-on",
        )
    )
    brightness = await adapter.execute(
        Command(
            id="command-brightness",
            device_id=light_id,
            command="set_brightness",
            value=50,
            unit="%",
            idempotency_key="intent-brightness",
        )
    )

    assert accepted.accepted is True
    assert brightness.accepted is True
    assert transport.published[-2].topic == "zigbee2mqtt/living_room/main_light/set"
    assert transport.published[-2].payload == b'{"state":"ON"}'
    assert transport.published[-1].payload == b'{"brightness":127}'


@pytest.mark.asyncio
async def test_duplicate_and_unsupported_commands_never_publish() -> None:
    transport, adapter = await build_adapter()
    await adapter.discover()

    first = Command(
        id="command-on",
        device_id="unassigned.living-room-main-light",
        command="turn_on",
        idempotency_key="same-intent",
    )
    await adapter.execute(first)
    duplicate = await adapter.execute(first)
    unsupported = await adapter.execute(
        Command(
            id="command-cover",
            device_id="unassigned.living-room-main-light",
            command="open",
            idempotency_key="unsupported-intent",
        )
    )

    assert duplicate.accepted is False
    assert unsupported.accepted is False
    assert len(transport.published) == 1


@pytest.mark.asyncio
async def test_disconnected_transport_never_claims_command_success() -> None:
    transport, adapter = await build_adapter()
    await adapter.discover()
    await adapter.disconnect()

    with pytest.raises(ConnectionError):
        await adapter.execute(
            Command(
                id="command-offline",
                device_id="unassigned.living-room-main-light",
                command="turn_off",
                idempotency_key="offline-intent",
            )
        )

    assert transport.published == []


@pytest.mark.asyncio
async def test_read_state_projects_typed_values_and_source_references() -> None:
    _transport, adapter = await build_adapter()
    await adapter.discover()

    states = await adapter.read_state(
        [
            SourceRef(
                adapter_id="zigbee2mqtt",
                external_id="living_room/main_light",
            )
        ]
    )

    values = {state.capability: state.value for state in states}
    assert values == {"power": True, "brightness": 50}
    assert all(state.status is StateStatus.CURRENT for state in states)
    assert all(state.source_ref.external_id == "living_room/main_light" for state in states)


@pytest.mark.asyncio
async def test_state_and_malformed_payloads_become_events_or_diagnostics() -> None:
    transport, adapter = await build_adapter()
    await adapter.discover()
    transport.incoming.append(
        state_message("zigbee2mqtt/living_room/main_light", {"brightness": 300})
    )
    transport.incoming.append(state_message("zigbee2mqtt/living_room/main_light", {"state": "OFF"}))
    transport.incoming.append(
        state_message("zigbee2mqtt/living_room/main_light/availability", {"state": "offline"})
    )
    transport.incoming.append(
        type(transport.incoming[0])(
            topic="zigbee2mqtt/living_room/main_light",
            payload=b"not-json",
            retained=False,
        )
    )

    subscription = adapter.subscribe_events()
    events = [await anext(subscription) for _ in range(4)]

    assert any(event.kind == "state_changed" for event in events)
    assert any(event.kind == "availability_changed" for event in events)
    assert any(event.kind == "adapter_diagnostic" for event in events)

    snapshot = await adapter.discover()
    light = next(entity for entity in snapshot.source_entities if entity["domain"] == "light")
    assert light["available"] is False
