"""Backward-compatible imports for the shared MQTT transport boundary.

The transport is protocol infrastructure, not Zigbee2MQTT behavior. Keeping
these aliases preserves existing integrations while preventing generic MQTT
adapters from depending on the Zigbee2MQTT package.
"""

from domoai.runtime.mqtt_transport import (
    AiomqttTransport,
    InMemoryMqttTransport,
    MqttMessage,
    MqttPublish,
    MqttTransport,
)

__all__ = [
    "AiomqttTransport",
    "InMemoryMqttTransport",
    "MqttMessage",
    "MqttPublish",
    "MqttTransport",
]
