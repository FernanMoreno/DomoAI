"""Deterministic KNX mapping and transport data for local tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from domoai.adapters.knx.transport import KnxGroupValue


def mapping_payload(*, count: int = 5) -> dict[str, Any]:
    entities: list[dict[str, Any]] = [
        {
            "entity_id": "living_room.main_light",
            "name": "Main Light",
            "area_id": "living_room",
            "semantic_type": "light",
            "manufacturer": "Fixture KNX",
            "model": "Dimmer 1",
            "capabilities": [
                {
                    "name": "power",
                    "dpt": "1.001",
                    "state_group_address": "1/0/1",
                    "command_group_address": "1/0/0",
                },
                {
                    "name": "brightness",
                    "dpt": "5.001",
                    "state_group_address": "1/0/3",
                    "command_group_address": "1/0/2",
                },
            ],
        },
        {
            "entity_id": "bedroom.switch",
            "name": "Bedroom Switch",
            "area_id": "bedroom",
            "semantic_type": "switch",
            "manufacturer": "Fixture KNX",
            "model": "Switch 1",
            "capabilities": [
                {
                    "name": "power",
                    "dpt": "1.001",
                    "state_group_address": "2/0/1",
                    "command_group_address": "2/0/0",
                }
            ],
        },
        {
            "entity_id": "bedroom.environment",
            "name": "Bedroom Environment",
            "area_id": "bedroom",
            "semantic_type": "sensor",
            "manufacturer": "Fixture KNX",
            "model": "Sensor 1",
            "capabilities": [
                {
                    "name": "temperature",
                    "dpt": "9.001",
                    "state_group_address": "2/1/1",
                },
                {
                    "name": "humidity",
                    "dpt": "9.007",
                    "state_group_address": "2/1/2",
                },
                {
                    "name": "occupancy",
                    "dpt": "1.018",
                    "state_group_address": "2/1/3",
                },
            ],
        },
    ]
    for index in range(max(0, count - len(entities))):
        entities.append(
            {
                "entity_id": f"office.switch_{index}",
                "name": f"Office Switch {index}",
                "area_id": "office",
                "semantic_type": "switch",
                "capabilities": [
                    {
                        "name": "power",
                        "dpt": "1.001",
                        "state_group_address": f"3/0/{index + 1}",
                        "command_group_address": f"3/1/{index + 1}",
                    }
                ],
            }
        )
    return {"schema_version": "v1", "entities": entities}


def group_values() -> list[KnxGroupValue]:
    observed_at = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
    return [
        KnxGroupValue("1/0/1", "1.001", True, observed_at),
        KnxGroupValue("1/0/3", "5.001", 50, observed_at),
        KnxGroupValue("2/0/1", "1.001", False, observed_at),
        KnxGroupValue("2/1/1", "9.001", 21.5, observed_at),
        KnxGroupValue("2/1/2", "9.007", 42.5, observed_at),
        KnxGroupValue("2/1/3", "1.018", True, observed_at),
    ]


def updated_group_value(
    group_address: str,
    dpt: str,
    value: bool | int | float,
) -> KnxGroupValue:
    return KnxGroupValue(
        group_address=group_address,
        dpt=dpt,
        value=value,
        observed_at=datetime.now(UTC),
    )


def stale_group_value(group_address: str, dpt: str, value: bool | int | float) -> KnxGroupValue:
    return KnxGroupValue(
        group_address=group_address,
        dpt=dpt,
        value=value,
        observed_at=datetime.now(UTC) - timedelta(minutes=1),
    )
