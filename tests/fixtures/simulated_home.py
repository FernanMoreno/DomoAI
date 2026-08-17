"""Home Assistant-shaped deterministic source fixtures."""

from __future__ import annotations

from typing import Any


def simulated_home_entities() -> list[dict[str, Any]]:
    return [
        {
            "entity_id": "light.living_room_main",
            "domain": "light",
            "name": "Living room main light",
            "area_id": "living_room",
            "device_id": "ha-light-1",
            "manufacturer": "Fixture Lamps",
            "model": "L100",
            "supported_features": ["brightness"],
            "attributes": {"brightness_min": 0, "brightness_max": 100},
            "state": {"power": False, "brightness": 0},
        },
        {
            "entity_id": "switch.garden_pump",
            "domain": "switch",
            "name": "Garden pump",
            "area_id": "garden",
            "device_id": "ha-switch-1",
            "manufacturer": "Fixture Controls",
            "model": "S100",
            "supported_features": [],
            "attributes": {},
            "state": {"power": False},
        },
        {
            "entity_id": "cover.bedroom_blind",
            "domain": "cover",
            "name": "Bedroom blind",
            "area_id": "bedroom",
            "device_id": "ha-cover-1",
            "manufacturer": "Fixture Covers",
            "model": "C100",
            "supported_features": ["position", "open", "close", "stop"],
            "attributes": {"position_min": 0, "position_max": 100},
            "state": {"position": 50},
        },
        {
            "entity_id": "climate.bedroom",
            "domain": "climate",
            "name": "Bedroom climate",
            "area_id": "bedroom",
            "device_id": "ha-climate-1",
            "manufacturer": "Fixture Climate",
            "model": "T100",
            "supported_features": ["target_temperature"],
            "attributes": {"temperature_min": 16, "temperature_max": 27, "unit": "°C"},
            "state": {"temperature": 20, "target_temperature": 21},
        },
        {
            "entity_id": "sensor.living_room_temperature",
            "domain": "sensor",
            "name": "Living room temperature",
            "area_id": "living_room",
            "device_id": "ha-sensor-1",
            "manufacturer": "Fixture Sensors",
            "model": "E100",
            "supported_features": [],
            "attributes": {"unit": "°C", "measurement": "temperature"},
            "state": {"temperature": 20.5},
        },
        {
            "entity_id": "sensor.house_power",
            "domain": "sensor",
            "name": "House power",
            "area_id": "garage",
            "device_id": "ha-energy-1",
            "manufacturer": "Fixture Energy",
            "model": "P100",
            "supported_features": [],
            "attributes": {"unit": "W", "measurement": "power"},
            "state": {"power": 420},
        },
    ]
