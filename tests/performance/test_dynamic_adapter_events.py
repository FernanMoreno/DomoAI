from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
)
from domoai.runtime.managed_adapter_set import ManagedAdapterSet


class BurstAdapter:
    adapter_id = "fixture:burst"

    def __init__(self) -> None:
        self.connected = False
        self.events: asyncio.Queue[SourceEvent] = asyncio.Queue()

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def discover(self) -> AdapterSnapshot:
        return AdapterSnapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        return []

    async def execute(
        self, command: Command, execution_context: object | None = None
    ) -> AdapterExecutionAck:
        return AdapterExecutionAck(accepted=False)

    async def events_stream(self) -> AsyncIterator[SourceEvent]:
        while self.connected:
            yield await self.events.get()

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        return self.events_stream()

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self.connected)


@pytest.mark.asyncio
async def test_state_event_burst_remains_bounded_and_coalesces() -> None:
    adapter = BurstAdapter()
    managed = ManagedAdapterSet([adapter], event_queue_max_size=2)
    await managed.connect()
    stream = managed.subscribe_events()
    consumer = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    for value in range(100):
        await adapter.events.put(
            StateChangedEvent(
                source_adapter_id=adapter.adapter_id,
                payload={"entity_id": "light.burst", "capability": "power", "value": value},
            )
        )
    event = await asyncio.wait_for(consumer, timeout=1)
    assert event.payload["value"] == 99
    assert managed.coalesced_events_total > 0
    await stream.aclose()
    await managed.disconnect()
