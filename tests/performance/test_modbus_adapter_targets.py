from __future__ import annotations

import time

import pytest

from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.transport import InMemoryModbusTransport
from tests.fixtures.modbus import mapping_payload


@pytest.mark.asyncio
async def test_twenty_configured_entities_discover_under_one_second() -> None:
    adapter = ModbusAdapter(
        InMemoryModbusTransport(),
        ModbusMappingDocument.model_validate(mapping_payload(count=20)),
    )
    await adapter.connect()

    started = time.perf_counter()
    snapshot = await adapter.discover()
    elapsed = time.perf_counter() - started

    assert len(snapshot.source_entities) == 20
    assert elapsed < 1.0
