import time

import pytest

from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.domain.models import Command
from tests.fixtures.matter_server import event_message, node_snapshots, server_info


@pytest.mark.asyncio
async def test_twenty_matter_endpoints_discover_under_one_second() -> None:
    transport = InMemoryMatterTransport(nodes=node_snapshots(20), server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    await adapter.connect()

    started = time.perf_counter()
    snapshot = await adapter.discover()
    elapsed = time.perf_counter() - started

    assert len(snapshot.source_entities) == 20
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_matter_command_and_event_processing_under_one_second() -> None:
    transport = InMemoryMatterTransport(nodes=node_snapshots(1), server_info=server_info())
    adapter = MatterServerAdapter(transport, discovery_timeout=0.01)
    await adapter.connect()
    await adapter.discover()
    transport.enqueue(event_message("attribute_updated", [1001, "1/6/0", True]))
    started = time.perf_counter()
    await adapter.execute(
        Command(
            id="performance-command",
            device_id="unassigned.matter-fixture-1001",
            command="turn_on",
            idempotency_key="performance-intent",
        )
    )
    await anext(adapter.subscribe_events())

    assert time.perf_counter() - started < 1.0
