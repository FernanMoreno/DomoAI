from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.mapper import ModbusMapper
from domoai.adapters.modbus.transport import (
    InMemoryModbusTransport,
    PyModbusTcpTransport,
)
from domoai.domain.models import Command, SourceRef
from tests.fixtures.modbus import mapping_payload, samples


def test_mapping_projects_bounded_canonical_entities() -> None:
    document = ModbusMappingDocument.model_validate(mapping_payload())
    snapshot = ModbusMapper().to_snapshot(document)

    assert len(snapshot.source_entities) == 3
    light = next(entity for entity in snapshot.source_entities if entity["domain"] == "light")
    assert light["entity_id"] == "living_room.main_light"
    assert light["unit_id"] == 1
    assert {capability["name"] for capability in light["capabilities"]} == {
        "power",
        "brightness",
    }
    assert all(capability["writable"] for capability in light["capabilities"])


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda payload: payload["entities"][0].update({"unknown": True}), "extra"),
        (lambda payload: payload["entities"][0].update({"unit_id": 0}), "greater than or equal"),
        (
            lambda payload: payload["entities"][0]["capabilities"][0]["state"].update(
                {"address": "40001"}
            ),
            "integer",
        ),
        (
            lambda payload: payload["entities"][0]["capabilities"][0].update(
                {
                    "name": "temperature",
                    "state": {
                        "area": "input_register",
                        "address": 40,
                        "data_type": "int16",
                    },
                    "command": None,
                }
            ),
            "unsupported",
        ),
    ],
)
def test_mapping_rejects_unsafe_or_ambiguous_documents(
    mutator: Callable[[dict[str, Any]], Any], message: str
) -> None:
    payload = mapping_payload()
    mutator(payload)

    with pytest.raises(ValueError, match=message):
        ModbusMappingDocument.model_validate(payload)


def test_writable_sensor_and_conflicting_shared_points_are_rejected() -> None:
    writable_sensor = mapping_payload()
    sensor_binding = writable_sensor["entities"][2]["capabilities"][0]
    sensor_binding["command"] = {
        "area": "holding_register",
        "address": 50,
        "data_type": "int16",
    }
    with pytest.raises(ValueError, match="read-only"):
        ModbusMappingDocument.model_validate(writable_sensor)

    conflicting = mapping_payload()
    conflicting["entities"].append(
        {
            "entity_id": "office.temperature_copy",
            "name": "Temperature Copy",
            "area_id": "office",
            "semantic_type": "sensor",
            "unit_id": 1,
            "capabilities": [
                {
                    "name": "temperature",
                    "state": {
                        "area": "input_register",
                        "address": 20,
                        "data_type": "int16",
                        "scale": 1,
                    },
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="conflicting"):
        ModbusMappingDocument.model_validate(conflicting)


@pytest.mark.asyncio
async def test_adapter_reads_state_and_preserves_modbus_source_refs() -> None:
    adapter = ModbusAdapter(
        InMemoryModbusTransport(samples()),
        ModbusMappingDocument.model_validate(mapping_payload()),
    )
    await adapter.connect()
    snapshot = await adapter.discover()
    states = await adapter.read_state(
        [
            SourceRef(adapter_id="modbus", external_id="living_room.main_light"),
            SourceRef(adapter_id="modbus", external_id="bedroom.environment"),
        ]
    )

    assert len(snapshot.source_entities) == 3
    assert {(state.capability, state.value, state.unit) for state in states} == {
        ("power", True, None),
        ("brightness", 50, "%"),
        ("temperature", 21.5, "°C"),
        ("humidity", 42.5, "%"),
        ("occupancy", True, None),
    }
    assert all(state.source_ref.external_type == "modbus_unit:1" for state in states)


@pytest.mark.asyncio
async def test_adapter_writes_exact_bounded_values_and_rejects_duplicates() -> None:
    transport = InMemoryModbusTransport(samples())
    adapter = ModbusAdapter(transport, ModbusMappingDocument.model_validate(mapping_payload()))
    await adapter.connect()
    await adapter.discover()

    power = await adapter.execute(
        Command(
            id="modbus-command-on",
            device_id="living_room.main-light",
            command="turn_on",
            idempotency_key="modbus-intent-on",
        )
    )
    brightness = await adapter.execute(
        Command(
            id="modbus-command-brightness",
            device_id="living_room.main-light",
            command="set_brightness",
            value=60,
            unit="%",
            idempotency_key="modbus-intent-brightness",
        )
    )
    duplicate = await adapter.execute(
        Command(
            id="modbus-command-duplicate",
            device_id="living_room.main-light",
            command="turn_on",
            idempotency_key="modbus-intent-on",
        )
    )

    assert power.accepted is True
    assert brightness.accepted is True
    assert duplicate.accepted is False
    assert [
        (write.unit_id, write.area, write.address, write.values) for write in transport.writes
    ] == [
        (1, "coil", 1, (True,)),
        (1, "holding_register", 11, (60,)),
    ]


@pytest.mark.asyncio
async def test_invalid_and_unavailable_commands_never_write() -> None:
    transport = InMemoryModbusTransport(samples())
    adapter = ModbusAdapter(transport, ModbusMappingDocument.model_validate(mapping_payload()))
    await adapter.connect()
    await adapter.discover()

    invalid = await adapter.execute(
        Command(
            id="modbus-command-invalid",
            device_id="living_room.main-light",
            command="set_brightness",
            value=101,
            unit="%",
            idempotency_key="modbus-intent-invalid",
        )
    )
    unknown = await adapter.execute(
        Command(
            id="modbus-command-unknown",
            device_id="unknown.device",
            command="turn_on",
            idempotency_key="modbus-intent-unknown",
        )
    )
    transport.set_health(False)
    unavailable = await adapter.execute(
        Command(
            id="modbus-command-unavailable",
            device_id="living_room.main-light",
            command="turn_on",
            idempotency_key="modbus-intent-unavailable",
        )
    )

    assert invalid.accepted is False
    assert unknown.accepted is False
    assert unavailable.accepted is False
    assert transport.writes == []


class _FakeResponse:
    def __init__(
        self,
        *,
        bits: list[bool] | None = None,
        registers: list[int] | None = None,
    ) -> None:
        self.bits = bits or []
        self.registers = registers or []

    def isError(self) -> bool:
        return False


class _FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    async def read_coils(self, *args: object, **kwargs: object) -> _FakeResponse:
        self.calls.append(("read_coils", args, kwargs))
        return _FakeResponse(bits=[True])

    async def write_coil(self, *args: object, **kwargs: object) -> _FakeResponse:
        self.calls.append(("write_coil", args, kwargs))
        return _FakeResponse()


@pytest.mark.asyncio
async def test_live_transport_isolated_behind_fake_client() -> None:
    client = _FakeClient()
    transport = PyModbusTcpTransport(
        "192.0.2.20",
        client_factory=lambda _host, _port, _timeout: client,
    )
    await transport.connect()
    sample = await transport.read(1, "coil", 4, 1)
    await transport.write(1, "coil", 5, (False,))

    assert sample is not None
    assert sample.values == (True,)
    assert client.calls[0] == ("read_coils", (4, 1), {"slave": 1})
    assert client.calls[1] == ("write_coil", (5, False), {"slave": 1})
    await transport.disconnect()
