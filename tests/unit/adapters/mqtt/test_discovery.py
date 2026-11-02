import json

import pytest

from domoai.adapters.mqtt.discovery import MqttDiscoveryImporter
from domoai.runtime.mqtt_transport import InMemoryMqttTransport, MqttMessage


def _config(topic: str, payload: dict[str, object]) -> MqttMessage:
    return MqttMessage(topic, json.dumps(payload).encode(), retained=True)


@pytest.mark.asyncio
async def test_preview_imports_direct_topic_switch_without_publishing() -> None:
    transport = InMemoryMqttTransport(
        [
            _config(
                "homeassistant/switch/lamp/config",
                {
                    "unique_id": "lamp-1",
                    "name": "Lamp",
                    "state_topic": "house/lamp/state",
                    "command_topic": "house/lamp/set",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device": {"identifiers": ["lamp-1"]},
                },
            )
        ]
    )
    importer = MqttDiscoveryImporter(
        transport, discovery_prefix="homeassistant", adapter_id="mqtt-home"
    )

    preview = await importer.preview()

    assert preview.persistable is True
    assert preview.mapping is not None
    capability = preview.mapping.devices[0].capabilities[0]
    assert capability.kind.value == "boolean"
    assert capability.payload_on == "ON"
    assert capability.command_topic == "house/lamp/set"
    assert transport.published == []
    assert "homeassistant/#" in transport.subscriptions


@pytest.mark.asyncio
async def test_preview_rejects_templates_and_wildcard_writes() -> None:
    transport = InMemoryMqttTransport(
        [
            _config(
                "homeassistant/switch/unsafe/config",
                {
                    "unique_id": "unsafe",
                    "state_topic": "house/unsafe/state",
                    "command_topic": "house/+/set",
                    "value_template": "{{ value_json.state }}",
                },
            )
        ]
    )
    preview = await MqttDiscoveryImporter(transport).preview()

    assert preview.persistable is False
    assert preview.mapping is None
    assert {item.code for item in preview.diagnostics} >= {
        "mqtt_discovery_template_forbidden",
        "mqtt_discovery_wildcard_forbidden",
    }
    assert transport.published == []


@pytest.mark.asyncio
async def test_preview_rejects_unsupported_component_and_invalid_json() -> None:
    transport = InMemoryMqttTransport(
        [
            MqttMessage("homeassistant/cover/door/config", b"{}", retained=True),
            MqttMessage("homeassistant/sensor/temp/config", b"not-json", retained=True),
        ]
    )
    preview = await MqttDiscoveryImporter(transport).preview()

    assert preview.persistable is False
    assert {item.code for item in preview.diagnostics} >= {
        "mqtt_discovery_component_unsupported",
        "mqtt_discovery_payload_invalid",
    }
