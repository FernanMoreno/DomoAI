import pytest

from domoai.persistence.connection_documents import ConnectionDocumentRepository
from domoai.persistence.sqlite import SQLiteDatabase


@pytest.mark.asyncio
async def test_document_repository_deduplicates_by_kind_and_digest(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "domoai.db")
    await database.initialize()
    repository = ConnectionDocumentRepository(database)

    first = await repository.save(
        kind="mqtt_mapping",
        payload={"schema_version": "v1", "adapter_id": "mqtt-home", "devices": []},
        provenance={"topic": "homeassistant/light/lamp/config"},
    )
    second = await repository.save(
        kind="mqtt_mapping",
        payload={"devices": [], "adapter_id": "mqtt-home", "schema_version": "v1"},
        provenance={"topic": "homeassistant/light/lamp/config"},
    )

    assert first.document_ref == second.document_ref
    assert first.digest == second.digest
    assert await repository.get(first.document_ref) == first


@pytest.mark.asyncio
async def test_document_repository_rejects_sensitive_provenance(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "domoai.db")
    await database.initialize()
    repository = ConnectionDocumentRepository(database)

    with pytest.raises(ValueError, match="sensitive"):
        await repository.save(
            kind="mqtt_mapping",
            payload={"schema_version": "v1"},
            provenance={"password": "do-not-store"},
        )
