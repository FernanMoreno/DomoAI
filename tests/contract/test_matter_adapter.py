import pytest

from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.mapper import MatterMapper
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.domain.models import Command, SourceRef, StateStatus
from tests.fixtures.matter_server import (
    event_message,
    malformed_message,
    node_snapshot,
    node_snapshots,
    sensor_node,
    server_info,
)


@pytest.mark.asyncio
async def test_in_memory_transport_records_requests_and_delivers_events() -> None:
    transport = InMemoryMatterTransport(
        nodes=node_snapshots(1),
        server_info=server_info(),
    )

    await transport.connect()
    result = await transport.request("start_listening")
    transport.enqueue(event_message("server_info_updated", {"schema_version": 13}))

    assert result == node_snapshots(1)
    assert transport.requests[0].command == "start_listening"
    assert await transport.receive(0.01) == event_message(
        "server_info_updated", {"schema_version": 13}
    )


@pytest.mark.asyncio
async def test_matter_transport_rejects_incompatible_server_schema() -> None:
    transport = InMemoryMatterTransport(
        nodes=node_snapshots(1),
        server_info=server_info(schema_version=10, minimum_schema_version=11),
    )

    with pytest.raises(ConnectionError, match="schema range"):
        await transport.connect()


def test_mapper_projects_light_sensor_and_unsupported_endpoints() -> None:
    snapshot = MatterMapper().to_snapshot(
        [
            node_snapshot(1, profile="dimmable_light"),
            sensor_node(2),
            node_snapshot(3, profile="unknown"),
        ]
    )

    light = next(
        entity
        for entity in snapshot.source_entities
        if entity["entity_id"] == "node:1/endpoint:1"
    )
    sensor = next(
        entity
        for entity in snapshot.source_entities
        if entity["entity_id"] == "node:2/endpoint:1"
    )
    unsupported = next(
        source
        for source in snapshot.unsupported_sources
        if source["entity_id"] == "node:3/endpoint:1"
    )

    assert light["semantic_type"] == "light"
    assert {capability["name"] for capability in light["capabilities"]} == {"power", "brightness"}
    assert {capability["name"] for capability in sensor["capabilities"]} == {
        "temperature",
        "humidity",
        "occupancy",
    }
    assert unsupported["reason"] == "unsupported Matter device type"


def test_mapper_converts_attribute_units_and_rejects_invalid_levels() -> None:
    mapper = MatterMapper()
    node = sensor_node(2)
    states, diagnostics = mapper.map_states(node)

    assert {(state["capability"], state["value"], state["unit"]) for state in states} == {
        ("temperature", 21.5, "°C"),
        ("humidity", 42.5, "%"),
        ("occupancy", True, None),
    }
    assert diagnostics == []

    invalid = node_snapshot(4, profile="dimmable_light", level=255)
    _, invalid_diagnostics = mapper.map_states(invalid)
    assert invalid_diagnostics == ["brightness must be an integer between 0 and 254"]


@pytest.mark.asyncio
async def test_adapter_emits_typed_state_and_sanitized_diagnostics() -> None:
    transport = InMemoryMatterTransport(nodes=node_snapshots(1), server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    await adapter.connect()
    await adapter.discover()

    transport.enqueue(event_message("attribute_updated", [1001, "1/6/0", False]))
    state_event = await anext(adapter.subscribe_events())
    states = await adapter.read_state(
        [SourceRef(adapter_id="matter", external_id="node:1001/endpoint:1")]
    )

    assert state_event.kind == "state_changed"
    assert states[0].capability == "power"
    assert states[0].value is False
    assert states[0].status is StateStatus.CURRENT

    transport.enqueue(malformed_message())
    diagnostic = await anext(adapter.subscribe_events())
    assert diagnostic.kind == "adapter_diagnostic"
    assert "bad" not in str(diagnostic.payload)

    transport.enqueue(event_message("attribute_updated", [1001, "1/999/0", 1]))
    unknown_path = await anext(adapter.subscribe_events())
    assert unknown_path.kind == "adapter_diagnostic"

    transport.enqueue(event_message("attribute_updated", [1001, "1/8/0", 255]))
    invalid_value = await anext(adapter.subscribe_events())
    assert invalid_value.kind == "adapter_diagnostic"
    brightness = await adapter.read_state(
        [SourceRef(adapter_id="matter", external_id="node:1001/endpoint:1")]
    )
    assert next(state for state in brightness if state.capability == "brightness").value == 50


@pytest.mark.asyncio
async def test_adapter_publishes_exact_bounded_matter_commands_and_idempotency() -> None:
    transport = InMemoryMatterTransport(nodes=node_snapshots(1), server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    await adapter.connect()
    await adapter.discover()

    command = Command(
        id="matter-command-1",
        device_id="unassigned.matter-fixture-1001",
        command="set_brightness",
        value=60,
        unit="%",
        idempotency_key="matter-intent-1",
    )
    acknowledgement = await adapter.execute(command)
    duplicate = await adapter.execute(command.model_copy(update={"id": "matter-command-2"}))

    assert acknowledgement.accepted is True
    assert duplicate.accepted is False
    assert len(transport.requests) == 2
    request = transport.requests[1]
    assert request.command == "device_command"
    assert request.args == {
        "node_id": 1001,
        "endpoint_id": 1,
        "cluster_id": 8,
        "command_name": "MoveToLevelWithOnOff",
        "payload": {
            "level": 152,
            "transitionTime": 0,
            "optionsMask": 0,
            "optionsOverride": 0,
        },
    }


@pytest.mark.asyncio
async def test_adapter_rejects_unknown_or_out_of_range_commands_without_request() -> None:
    transport = InMemoryMatterTransport(nodes=node_snapshots(1), server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    await adapter.connect()
    await adapter.discover()

    unknown = await adapter.execute(
        Command(
            id="matter-command-unknown",
            device_id="unassigned.matter-fixture-9999",
            command="turn_on",
            idempotency_key="matter-intent-unknown",
        )
    )
    invalid = await adapter.execute(
        Command(
            id="matter-command-invalid",
            device_id="unassigned.matter-fixture-1001",
            command="set_brightness",
            value=101,
            unit="%",
            idempotency_key="matter-intent-invalid",
        )
    )

    assert unknown.accepted is False
    assert invalid.accepted is False
    assert all(request.command != "device_command" for request in transport.requests)
