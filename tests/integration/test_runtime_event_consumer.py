from collections.abc import AsyncIterator

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import SourceEvent, StateStatus
from domoai.runtime.event_consumer import RuntimeEventConsumer
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.matter_server import event_message, node_snapshot, server_info


@pytest.mark.asyncio
async def test_source_event_refreshes_canonical_state_and_revision() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    adapter.set_available("cover.bedroom_blind", False)
    event = await consumer.consume_once()

    cover_id = registry.canonical_id_for_source("fixture", "cover.bedroom_blind")
    assert event is not None
    assert event.kind == "availability_changed"
    assert cover_id is not None
    state = await state_store.get(cover_id, "position")
    assert state is not None
    assert state.status is StateStatus.UNAVAILABLE
    assert state_store.runtime_revision == "rev-2"
    assert audit.events[-1].event_type == "source_event_applied"


class DisconnectedAdapter(SimulatedHomeAdapter):
    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        raise ConnectionError("Home Assistant event stream disconnected")
        yield SourceEvent(kind="unreachable")


@pytest.mark.asyncio
async def test_disconnected_source_marks_cached_state_stale_and_audits() -> None:
    adapter = DisconnectedAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    event = await consumer.consume_once()

    assert event is None
    snapshots = await state_store.all()
    assert snapshots
    assert all(snapshot.status is StateStatus.STALE for snapshot in snapshots)
    assert audit.events[-1].event_type == "source_event_stream_unavailable"


@pytest.mark.asyncio
async def test_matter_event_refreshes_canonical_runtime_state() -> None:
    nodes = [node_snapshot(1001)]
    transport = InMemoryMatterTransport(nodes=nodes, server_info=server_info())
    adapter = MatterServerAdapter(transport)
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await adapter.connect()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)

    nodes[0] = node_snapshot(1001, available=False, on=False)
    transport.enqueue(event_message("node_updated", nodes[0]))
    event = await consumer.consume_once()

    device_id = registry.canonical_id_for_source("matter", "node:1001/endpoint:1")
    assert event is not None
    assert event.kind == "availability_changed"
    assert device_id is not None
    state = await state_store.get(device_id, "power")
    assert state is not None
    assert state.status is StateStatus.UNAVAILABLE
