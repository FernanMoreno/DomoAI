"""Deterministic Zigbee2MQTT MQTT messages for adapter tests."""

from __future__ import annotations

import json
from hashlib import sha1
from typing import Any

from domoai.adapters.zigbee2mqtt.transport import MqttMessage


def device_definition(
    friendly_name: str,
    *,
    kind: str = "light",
    supported: bool = True,
) -> dict[str, Any]:
    if kind == "light":
        exposes = [
            {
                "type": "light",
                "features": [
                    {"type": "binary", "name": "state", "property": "state"},
                    {
                        "type": "numeric",
                        "name": "brightness",
                        "property": "brightness",
                        "value_min": 0,
                        "value_max": 254,
                    },
                ],
            }
        ]
        model = "ZB-LIGHT-1"
        vendor = "Fixture Lamps"
    elif kind == "switch":
        exposes = [{"type": "switch", "name": "state", "property": "state"}]
        model = "ZB-SWITCH-1"
        vendor = "Fixture Controls"
    else:
        exposes = [
            {
                "type": "numeric",
                "name": "temperature",
                "property": "temperature",
                "unit": "°C",
            },
            {
                "type": "numeric",
                "name": "humidity",
                "property": "humidity",
                "unit": "%",
            },
            {"type": "binary", "name": "occupancy", "property": "occupancy"},
        ]
        model = "ZB-SENSOR-1"
        vendor = "Fixture Sensors"
    return {
        "ieee_address": f"0x00158d{sha1(friendly_name.encode()).hexdigest()[:10]}",
        "type": "Router",
        "supported": supported,
        "disabled": False,
        "friendly_name": friendly_name,
        "model_id": model,
        "definition": {
            "source": "native",
            "model": model,
            "vendor": vendor,
            "description": f"Fixture {kind}",
            "exposes": exposes,
        },
    }


def bridge_devices(count: int = 3) -> list[dict[str, Any]]:
    devices = [
        device_definition("living_room/main_light", kind="light"),
        device_definition("garden/pump", kind="switch"),
        device_definition("living_room/environment", kind="sensor"),
    ]
    for index in range(len(devices), count):
        devices.append(device_definition(f"fixture/light_{index:02d}", kind="light"))
    return devices[:count]


def json_message(topic: str, payload: Any, *, retained: bool = False) -> MqttMessage:
    return MqttMessage(
        topic=topic,
        payload=json.dumps(payload, separators=(",", ":")).encode(),
        retained=retained,
    )


def retained_messages(count: int = 3) -> list[MqttMessage]:
    return [
        json_message("zigbee2mqtt/bridge/state", {"state": "online"}, retained=True),
        json_message("zigbee2mqtt/bridge/devices", bridge_devices(count), retained=True),
        json_message(
            "zigbee2mqtt/living_room/main_light",
            {"state": "ON", "brightness": 127},
            retained=True,
        ),
        json_message("zigbee2mqtt/garden/pump", {"state": "OFF"}, retained=True),
        json_message(
            "zigbee2mqtt/living_room/environment",
            {"temperature": 21.5, "humidity": 42, "occupancy": True},
            retained=True,
        ),
        json_message(
            "zigbee2mqtt/living_room/main_light/availability",
            {"state": "online"},
            retained=True,
        ),
    ]


def state_message(topic: str, payload: Any) -> MqttMessage:
    return json_message(topic, payload)
