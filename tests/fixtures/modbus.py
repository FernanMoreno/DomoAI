"""Deterministic Modbus mapping and raw values for local tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from domoai.adapters.modbus.config import ModbusArea
from domoai.adapters.modbus.transport import ModbusSample


def mapping_payload(*, count: int = 3) -> dict[str, Any]:
    entities: list[dict[str, Any]] = [
        {
            "entity_id": "living_room.main_light",
            "name": "Main Light",
            "area_id": "living_room",
            "semantic_type": "light",
            "manufacturer": "Fixture Modbus",
            "model": "Dimmer 1",
            "unit_id": 1,
            "capabilities": [
                {
                    "name": "power",
                    "state": {"area": "coil", "address": 0, "data_type": "bool"},
                    "command": {"area": "coil", "address": 1, "data_type": "bool"},
                },
                {
                    "name": "brightness",
                    "state": {
                        "area": "holding_register",
                        "address": 10,
                        "data_type": "uint16",
                    },
                    "command": {
                        "area": "holding_register",
                        "address": 11,
                        "data_type": "uint16",
                    },
                },
            ],
        },
        {
            "entity_id": "bedroom.switch",
            "name": "Bedroom Switch",
            "area_id": "bedroom",
            "semantic_type": "switch",
            "unit_id": 2,
            "capabilities": [
                {
                    "name": "power",
                    "state": {"area": "coil", "address": 2, "data_type": "bool"},
                    "command": {"area": "coil", "address": 3, "data_type": "bool"},
                }
            ],
        },
        {
            "entity_id": "bedroom.environment",
            "name": "Bedroom Environment",
            "area_id": "bedroom",
            "semantic_type": "sensor",
            "unit_id": 1,
            "capabilities": [
                {
                    "name": "temperature",
                    "state": {
                        "area": "input_register",
                        "address": 20,
                        "data_type": "int16",
                        "scale": 0.1,
                    },
                },
                {
                    "name": "humidity",
                    "state": {
                        "area": "input_register",
                        "address": 21,
                        "data_type": "uint16",
                        "scale": 0.1,
                    },
                },
                {
                    "name": "occupancy",
                    "state": {
                        "area": "discrete_input",
                        "address": 22,
                        "data_type": "bool",
                    },
                },
            ],
        },
    ]
    for index in range(max(0, count - len(entities))):
        address = 30 + index
        entities.append(
            {
                "entity_id": f"office.switch_{index}",
                "name": f"Office Switch {index}",
                "area_id": "office",
                "semantic_type": "switch",
                "unit_id": 3,
                "capabilities": [
                    {
                        "name": "power",
                        "state": {"area": "coil", "address": address, "data_type": "bool"},
                        "command": {
                            "area": "coil",
                            "address": address + 100,
                            "data_type": "bool",
                        },
                    }
                ],
            }
        )
    return {"schema_version": "v1", "entities": entities}


def samples() -> list[ModbusSample]:
    observed_at = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
    return [
        ModbusSample(1, "coil", 0, (True,), observed_at),
        ModbusSample(1, "holding_register", 10, (50,), observed_at),
        ModbusSample(2, "coil", 2, (False,), observed_at),
        ModbusSample(1, "input_register", 20, (215,), observed_at),
        ModbusSample(1, "input_register", 21, (425,), observed_at),
        ModbusSample(1, "discrete_input", 22, (True,), observed_at),
    ]


def updated_sample(
    unit_id: int,
    area: ModbusArea,
    address: int,
    values: tuple[bool | int, ...],
) -> ModbusSample:
    return ModbusSample(unit_id, area, address, values, datetime.now(UTC))
