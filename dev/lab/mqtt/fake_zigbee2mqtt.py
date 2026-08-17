"""Small MQTT boundary emulator for the DomoAI Zigbee2MQTT adapter.

It intentionally emulates only the topics DomoAI consumes. It is not a Zigbee
coordinator and must never be used as one.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

BASE_TOPIC = os.getenv("Z2M_BASE_TOPIC", "zigbee2mqtt").strip("/") or "zigbee2mqtt"


def device_definitions() -> list[dict[str, Any]]:
    return [
        {
            "ieee_address": "0x00158dvirtual0001",
            "type": "Router",
            "supported": True,
            "disabled": False,
            "friendly_name": "living_room/main_light",
            "model_id": "VIRTUAL-LIGHT-1",
            "definition": {
                "model": "VIRTUAL-LIGHT-1",
                "vendor": "DomoAI Lab",
                "description": "Virtual dimmable light",
                "exposes": [
                    {
                        "type": "light",
                        "features": [
                            {"type": "binary", "property": "state"},
                            {"type": "numeric", "property": "brightness"},
                        ],
                    }
                ],
            },
        },
        {
            "ieee_address": "0x00158dvirtual0002",
            "type": "Router",
            "supported": True,
            "disabled": False,
            "friendly_name": "garden/pump",
            "model_id": "VIRTUAL-SWITCH-1",
            "definition": {
                "model": "VIRTUAL-SWITCH-1",
                "vendor": "DomoAI Lab",
                "description": "Virtual switch",
                "exposes": [{"type": "switch", "property": "state"}],
            },
        },
        {
            "ieee_address": "0x00158dvirtual0003",
            "type": "EndDevice",
            "supported": True,
            "disabled": False,
            "friendly_name": "living_room/environment",
            "model_id": "VIRTUAL-SENSOR-1",
            "definition": {
                "model": "VIRTUAL-SENSOR-1",
                "vendor": "DomoAI Lab",
                "description": "Virtual environment sensor",
                "exposes": [
                    {"type": "numeric", "property": "temperature", "unit": "°C"},
                    {"type": "numeric", "property": "humidity", "unit": "%"},
                    {"type": "binary", "property": "occupancy"},
                ],
            },
        },
    ]


@dataclass
class VirtualBridge:
    """Pure state machine used by the MQTT process and deterministic tests."""

    base_topic: str = BASE_TOPIC
    states: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "living_room/main_light": {"state": "ON", "brightness": 127},
            "garden/pump": {"state": "OFF"},
            "living_room/environment": {
                "temperature": 21.5,
                "humidity": 45.0,
                "occupancy": True,
            },
        }
    )

    def topic(self, relative: str) -> str:
        return f"{self.base_topic}/{relative.lstrip('/')}"

    def initial_messages(self) -> list[tuple[str, bytes, bool]]:
        messages = [
            (self.topic("bridge/state"), _json({"state": "online"}), True),
            (self.topic("bridge/devices"), _json(device_definitions()), True),
        ]
        messages.extend(
            (self.topic(name), _json(payload), True)
            for name, payload in self.states.items()
        )
        messages.extend(
            (self.topic(f"{name}/availability"), _json({"state": "online"}), True)
            for name in self.states
        )
        return messages

    def apply_set(self, friendly_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if friendly_name not in self.states:
            raise ValueError(f"unknown virtual Zigbee2MQTT device: {friendly_name}")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("virtual Zigbee2MQTT set payload must be a non-empty object")
        state = self.states[friendly_name]
        allowed = set(state)
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unsupported virtual Zigbee2MQTT fields: {sorted(unknown)}")
        if "state" in payload and payload["state"] not in {"ON", "OFF"}:
            raise ValueError("virtual Zigbee2MQTT state must be ON or OFF")
        if "brightness" in payload:
            brightness = payload["brightness"]
            if isinstance(brightness, bool) or not isinstance(brightness, (int, float)):
                raise ValueError("virtual Zigbee2MQTT brightness must be numeric")
            if not 0 <= brightness <= 254:
                raise ValueError("virtual Zigbee2MQTT brightness must be 0..254")
        state.update(payload)
        return dict(state)


def _json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _connect_failed(reason_code: Any) -> bool:
    """Handle Paho v2 ReasonCode objects and legacy integer callbacks."""

    failure = getattr(reason_code, "is_failure", None)
    if failure is not None:
        return bool(failure)
    return reason_code != 0


def run() -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as error:  # pragma: no cover - exercised in container
        raise SystemExit("paho-mqtt is required for the virtual bridge") from error

    host = os.getenv("MQTT_HOST", "127.0.0.1")
    port = int(os.getenv("MQTT_PORT", "1883"))
    username = os.getenv("MQTT_USERNAME") or None
    password = os.getenv("MQTT_PASSWORD") or None
    bridge = VirtualBridge()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="domoai-lab-z2m")
    if username:
        client.username_pw_set(username, password)

    def publish_initial() -> None:
        for topic, payload, retained in bridge.initial_messages():
            client.publish(topic, payload=payload, retain=retained)

    def on_connect(
        _client: Any,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any,
    ) -> None:
        if _connect_failed(reason_code):
            return
        # Friendly names may contain slashes, so a single-level `+` filter is
        # insufficient for the same topic shape the adapter consumes.
        client.subscribe(f"{bridge.base_topic}/#")
        publish_initial()

    def on_message(_client: Any, _userdata: Any, message: Any) -> None:
        relative = str(message.topic)[len(bridge.base_topic) + 1 :]
        if not relative.endswith("/set"):
            return
        friendly_name = relative[: -len("/set")]
        try:
            payload = json.loads(bytes(message.payload).decode())
            state = bridge.apply_set(friendly_name, payload)
        except (ValueError, json.JSONDecodeError):
            return
        client.publish(bridge.topic(friendly_name), payload=_json(state), retain=True)

    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(host, port, keepalive=30)
            client.loop_forever()
            return
        except OSError:
            time.sleep(2)


if __name__ == "__main__":
    run()
