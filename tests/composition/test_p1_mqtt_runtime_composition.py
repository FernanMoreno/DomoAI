import json

import pytest

from domoai.adapters.mqtt.adapter import GenericMqttAdapter
from domoai.adapters.mqtt.config import MqttAdapterMapping
from domoai.adapters.mqtt.discovery import MqttDiscoveryImporter
from domoai.persistence.connection_documents import ConnectionDocumentRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.runtime.mqtt_transport import InMemoryMqttTransport, MqttMessage


@pytest.mark.composition
@pytest.mark.asyncio
async def test_mqtt_discovery_document_reaches_only_declared_runtime_adapter(tmp_path) -> None:
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
    database = SQLiteDatabase(tmp_path / "domoai.sqlite3")
    await database.initialize()
    importer = MqttDiscoveryImporter(
        transport,
        repository=ConnectionDocumentRepository(database),
    )

    preview = await importer.preview()
    stored = await importer.persist(preview, confirmed=True)
    payload = (
        stored.payload
        if hasattr(stored, "payload")
        else preview.mapping.model_dump(mode="json")
    )
    adapter = GenericMqttAdapter(transport, MqttAdapterMapping.model_validate(payload))
    runtime = ManagedAdapterSet()

    await runtime.attach(adapter)
    snapshot = await adapter.discover()

    assert snapshot.source_entities[0]["entity_id"] == "mqtt:lamp"
    assert transport.published == []
    assert await runtime.detach(adapter.adapter_id) is True
    await database.close()
