from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.codec import encode_point
from domoai.adapters.modbus.config import ModbusMappingDocument, ModbusPoint
from domoai.adapters.modbus.mapper import ModbusMapper
from domoai.adapters.modbus.transport import (
    InMemoryModbusTransport,
    ModbusSample,
    PyModbusTcpTransport,
)
from domoai.domain.models import Command, SourceRef
from tests.fixtures.modbus import mapping_payload, samples


def battery_mapping_payload() -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "entities": [
            {
                "entity_id": "lab.battery",
                "name": "Virtual Battery",
                "area_id": "lab",
                "semantic_type": "energy",
                "manufacturer": "DomoAI Lab",
                "model": "Deterministic Battery",
                "unit_id": 1,
                "capabilities": [
                    {
                        "name": "battery.soc",
                        "state": {
                            "area": "input_register",
                            "address": 0,
                            "data_type": "float32",
                        },
                    },
                    {
                        "name": "battery.power",
                        "state": {
                            "area": "input_register",
                            "address": 2,
                            "data_type": "float32",
                        },
                        "command": {
                            "area": "holding_register",
                            "address": 10,
                            "data_type": "float32",
                        },
                    },
                    {
                        "name": "battery.capacity",
                        "state": {
                            "area": "input_register",
                            "address": 4,
                            "data_type": "float32",
                        },
                    },
                ],
            }
        ],
    }


def water_meter_mapping_payload(*, second_entity: bool = False) -> dict[str, Any]:
    def entity(entity_id: str, unit_id: int, name: str) -> dict[str, Any]:
        return {
            "entity_id": entity_id,
            "name": name,
            "area_id": "lab",
            "semantic_type": "energy",
            "manufacturer": "DomoAI Lab",
            "model": "Deterministic Water Meter",
            "unit_id": unit_id,
            "capabilities": [
                {
                    "name": "water.flow_rate",
                    "state": {
                        "area": "input_register",
                        "address": 0,
                        "data_type": "float32",
                    },
                },
                {
                    "name": "water.total_volume",
                    "state": {
                        "area": "input_register",
                        "address": 2,
                        "data_type": "float32",
                    },
                },
            ],
        }

    entities = [entity("lab.water_meter", 1, "Virtual Water Meter")]
    if second_entity:
        entities.append(entity("lab.water_meter_2", 2, "Virtual Water Meter 2"))
    return {"schema_version": "v1", "entities": entities}


def battery_samples() -> list[ModbusSample]:
    def point(address: int) -> ModbusPoint:
        return ModbusPoint(area="input_register", address=address, data_type="float32")

    observed_at = samples()[0].observed_at
    return [
        ModbusSample(1, "input_register", 0, encode_point(point(0), 5.0), observed_at),
        ModbusSample(1, "input_register", 2, encode_point(point(2), 0.0), observed_at),
        ModbusSample(1, "input_register", 4, encode_point(point(4), 10.0), observed_at),
    ]


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


def test_mapping_projects_explicit_energy_capabilities_and_routes() -> None:
    document = ModbusMappingDocument.model_validate(battery_mapping_payload())
    snapshot = ModbusMapper().to_snapshot(document)

    battery = snapshot.source_entities[0]
    assert battery["domain"] == "energy"
    capabilities = {item["name"]: item for item in battery["capabilities"]}
    assert capabilities["battery.soc"]["unit"] == "kWh"
    assert capabilities["battery.capacity"]["writable"] is False
    assert capabilities["battery.power"]["commands"] == [
        "charge_battery",
        "discharge_battery",
        "stop_battery",
    ]


def test_mapping_projects_read_only_water_capabilities() -> None:
    document = ModbusMappingDocument.model_validate(water_meter_mapping_payload())
    snapshot = ModbusMapper().to_snapshot(document)

    meter = snapshot.source_entities[0]
    capabilities = {item["name"]: item for item in meter["capabilities"]}
    assert capabilities["water.flow_rate"]["unit"] == "L/min"
    assert capabilities["water.flow_rate"]["writable"] is False
    assert capabilities["water.total_volume"]["unit"] == "L"
    assert capabilities["water.total_volume"]["writable"] is False


def test_water_capability_rejects_a_command() -> None:
    payload = water_meter_mapping_payload()
    payload["entities"][0]["capabilities"][0]["command"] = {
        "area": "holding_register",
        "address": 10,
        "data_type": "float32",
    }

    with pytest.raises(ValueError, match="read-only"):
        ModbusMappingDocument.model_validate(payload)


def test_two_water_meter_entities_project_independently() -> None:
    # Spec 163 analysis finding E1 / FR-008 / SC-004 / Edge Case 3.
    document = ModbusMappingDocument.model_validate(
        water_meter_mapping_payload(second_entity=True)
    )
    snapshot = ModbusMapper().to_snapshot(document)

    entity_ids = {entity["entity_id"] for entity in snapshot.source_entities}
    assert entity_ids == {"lab.water_meter", "lab.water_meter_2"}
    unit_ids = {entity["entity_id"]: entity["unit_id"] for entity in snapshot.source_entities}
    assert unit_ids == {"lab.water_meter": 1, "lab.water_meter_2": 2}


def thermal_mapping_payload(*, second_entity: bool = False) -> dict[str, Any]:
    def entity(entity_id: str, unit_id: int, name: str) -> dict[str, Any]:
        return {
            "entity_id": entity_id,
            "name": name,
            "area_id": "lab",
            "semantic_type": "energy",
            "manufacturer": "DomoAI Lab",
            "model": "Deterministic Thermostat",
            "unit_id": unit_id,
            "capabilities": [
                {
                    "name": "thermal.indoor_temperature",
                    "state": {
                        "area": "input_register",
                        "address": 0,
                        "data_type": "float32",
                    },
                },
                {
                    "name": "thermal.hvac_power",
                    "state": {
                        "area": "input_register",
                        "address": 2,
                        "data_type": "float32",
                    },
                    "command": {
                        "area": "holding_register",
                        "address": 10,
                        "data_type": "float32",
                    },
                },
            ],
        }

    entities = [entity("lab.thermostat", 1, "Virtual Thermostat")]
    if second_entity:
        entities.append(entity("lab.thermostat_2", 2, "Virtual Thermostat 2"))
    return {"schema_version": "v1", "entities": entities}


def thermal_samples() -> list[ModbusSample]:
    def point(address: int) -> ModbusPoint:
        return ModbusPoint(area="input_register", address=address, data_type="float32")

    observed_at = samples()[0].observed_at
    return [
        ModbusSample(1, "input_register", 0, encode_point(point(0), 20.0), observed_at),
        ModbusSample(1, "input_register", 2, encode_point(point(2), 0.0), observed_at),
    ]


def test_mapping_projects_thermal_capabilities_with_hvac_power_dual_purpose() -> None:
    # Spec 165 T023a: corrects analysis finding M1's original framing --
    # thermal.hvac_power mirrors battery.power/ev_charging (dual-purpose
    # readable+writable), not water's pure read-only pattern, because
    # HVACActuator.capability and .power_feedback_capability point at the
    # same canonical capability, same as BatteryActuator/EVActuator do.
    document = ModbusMappingDocument.model_validate(thermal_mapping_payload())
    snapshot = ModbusMapper().to_snapshot(document)

    thermostat = snapshot.source_entities[0]
    capabilities = {item["name"]: item for item in thermostat["capabilities"]}
    assert capabilities["thermal.indoor_temperature"]["writable"] is False
    assert capabilities["thermal.hvac_power"]["commands"] == [
        "heat_thermostat",
        "cool_thermostat",
        "stop_thermostat",
    ]


def test_thermal_indoor_temperature_rejects_a_command() -> None:
    payload = thermal_mapping_payload()
    payload["entities"][0]["capabilities"][0]["command"] = {
        "area": "holding_register",
        "address": 10,
        "data_type": "float32",
    }

    with pytest.raises(ValueError, match="read-only"):
        ModbusMappingDocument.model_validate(payload)


def test_two_thermal_entities_project_independently() -> None:
    document = ModbusMappingDocument.model_validate(
        thermal_mapping_payload(second_entity=True)
    )
    snapshot = ModbusMapper().to_snapshot(document)

    entity_ids = {entity["entity_id"] for entity in snapshot.source_entities}
    assert entity_ids == {"lab.thermostat", "lab.thermostat_2"}
    unit_ids = {entity["entity_id"]: entity["unit_id"] for entity in snapshot.source_entities}
    assert unit_ids == {"lab.thermostat": 1, "lab.thermostat_2": 2}


@pytest.mark.asyncio
async def test_adapter_translates_hvac_dispatch_to_signed_power_setpoint() -> None:
    # Mirrors test_adapter_translates_battery_dispatch_to_signed_power_setpoint
    # exactly: heat -> positive, cool -> negative, stop -> 0.0.
    transport = InMemoryModbusTransport(thermal_samples())
    adapter = ModbusAdapter(
        transport, ModbusMappingDocument.model_validate(thermal_mapping_payload())
    )
    await adapter.connect()
    await adapter.discover()

    heat = await adapter.execute(
        Command(
            id="hvac-heat",
            device_id="lab.virtual-thermostat",
            command="heat_thermostat",
            value=2.0,
            unit="kW",
            idempotency_key="hvac-heat-1",
        )
    )
    cool = await adapter.execute(
        Command(
            id="hvac-cool",
            device_id="lab.virtual-thermostat",
            command="cool_thermostat",
            value=1.5,
            unit="kW",
            idempotency_key="hvac-cool-1",
        )
    )
    stop = await adapter.execute(
        Command(
            id="hvac-stop",
            device_id="lab.virtual-thermostat",
            command="stop_thermostat",
            idempotency_key="hvac-stop-1",
        )
    )

    assert heat.accepted and cool.accepted and stop.accepted
    point = ModbusPoint(area="holding_register", address=10, data_type="float32")
    assert [transport.writes[index].values for index in range(len(transport.writes))] == [
        encode_point(point, 2.0),
        encode_point(point, -1.5),
        encode_point(point, 0.0),
    ]


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


@pytest.mark.asyncio
async def test_adapter_translates_battery_dispatch_to_signed_power_setpoint() -> None:
    transport = InMemoryModbusTransport(battery_samples())
    adapter = ModbusAdapter(
        transport, ModbusMappingDocument.model_validate(battery_mapping_payload())
    )
    await adapter.connect()
    await adapter.discover()

    charge = await adapter.execute(
        Command(
            id="battery-charge",
            device_id="lab.virtual-battery",
            command="charge_battery",
            value=2.0,
            unit="kW",
            idempotency_key="battery-charge-1",
        )
    )
    discharge = await adapter.execute(
        Command(
            id="battery-discharge",
            device_id="lab.virtual-battery",
            command="discharge_battery",
            value=1.5,
            unit="kW",
            idempotency_key="battery-discharge-1",
        )
    )
    stop = await adapter.execute(
        Command(
            id="battery-stop",
            device_id="lab.virtual-battery",
            command="stop_battery",
            idempotency_key="battery-stop-1",
        )
    )

    assert charge.accepted and discharge.accepted and stop.accepted
    point = ModbusPoint(area="holding_register", address=10, data_type="float32")
    assert [transport.writes[index].values for index in range(len(transport.writes))] == [
        encode_point(point, 2.0),
        encode_point(point, -1.5),
        encode_point(point, 0.0),
    ]


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


class _ModernFakeClient:
    connected = True

    async def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    async def read_coils(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> _FakeResponse:
        assert (address, count, device_id) == (4, 1, 1)
        return _FakeResponse(bits=[True])


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


@pytest.mark.asyncio
async def test_live_transport_supports_current_pymodbus_read_signature() -> None:
    transport = PyModbusTcpTransport(
        "192.0.2.20",
        client_factory=lambda _host, _port, _timeout: _ModernFakeClient(),
    )
    await transport.connect()

    sample = await transport.read(1, "coil", 4, 1)

    assert sample is not None
    assert sample.values == (True,)
    await transport.disconnect()
