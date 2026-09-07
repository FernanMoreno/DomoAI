"""Deterministic ESP-like generic MQTT fixture."""

from __future__ import annotations

from domoai.adapters.mqtt.config import MqttAdapterMapping
from domoai.adapters.zigbee2mqtt.transport import InMemoryMqttTransport, MqttMessage


def mapping() -> MqttAdapterMapping:
    return MqttAdapterMapping.model_validate(
        {
            "schema_version": "v1",
            "adapter_id": "mqtt",
            "devices": [
                {
                    "source_id": "esp.lamp",
                    "type": "light",
                    "capabilities": [
                        {
                            "name": "power",
                            "state_topic": "home/esp-lamp/state",
                            "command_topic": "home/esp-lamp/set",
                            "feedback_topic": "home/esp-lamp/state",
                            "require_readback": True,
                        }
                    ],
                }
            ],
        }
    )


def transport() -> InMemoryMqttTransport:
    return InMemoryMqttTransport(
        [MqttMessage(topic="home/esp-lamp/state", payload=b"false", retained=True)]
    )
