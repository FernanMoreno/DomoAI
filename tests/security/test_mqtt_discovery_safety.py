import json

import pytest

from domoai.adapters.mqtt.discovery import MqttDiscoveryImporter
from domoai.runtime.mqtt_transport import InMemoryMqttTransport, MqttMessage


@pytest.mark.asyncio
async def test_persist_requires_explicit_confirmation_and_preview_is_read_only() -> None:
    transport = InMemoryMqttTransport(
        [
            MqttMessage(
                "homeassistant/switch/lamp/config",
                json.dumps(
                    {
                        "unique_id": "lamp",
                        "state_topic": "house/lamp/state",
                        "command_topic": "house/lamp/set",
                    }
                ).encode(),
                retained=True,
            )
        ]
    )
    importer = MqttDiscoveryImporter(transport)
    preview = await importer.preview()

    with pytest.raises(PermissionError, match="confirmation"):
        await importer.persist(preview)
    assert transport.published == []
