from __future__ import annotations

import time

import pytest

from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import KnxMappingDocument
from domoai.adapters.knx.transport import InMemoryKnxTransport
from tests.fixtures.knx import mapping_payload


@pytest.mark.asyncio
async def test_twenty_configured_entities_discover_under_one_second() -> None:
    adapter = KnxAdapter(
        InMemoryKnxTransport(),
        KnxMappingDocument.model_validate(mapping_payload(count=20)),
    )
    await adapter.connect()

    started = time.perf_counter()
    snapshot = await adapter.discover()
    elapsed = time.perf_counter() - started

    assert len(snapshot.source_entities) == 20
    assert elapsed < 1.0
