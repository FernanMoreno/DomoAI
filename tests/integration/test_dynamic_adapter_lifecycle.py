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
    StateSnapshot,
)
from domoai.runtime.managed_adapter_set import ManagedAdapterSet


class LifecycleAdapter:
    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.connected = False
        self.disconnect_count = 0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1

    async def discover(self) -> AdapterSnapshot:
        return AdapterSnapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        return []

    async def execute(
        self, command: Command, execution_context: object | None = None
    ) -> AdapterExecutionAck:
        return AdapterExecutionAck(accepted=False)

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        async def events() -> AsyncIterator[SourceEvent]:
            while self.connected:
                await asyncio.sleep(60)
                if False:
                    yield SourceEvent  # type: ignore[misc]

        return events()

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self.connected)


@pytest.mark.asyncio
async def test_five_children_attach_replace_detach_without_restart() -> None:
    managed = ManagedAdapterSet()
    await managed.connect()
    children = [LifecycleAdapter(f"fixture:{index}") for index in range(5)]
    for child in children:
        await managed.attach(child)
    assert {child.adapter_id for child in managed.adapters} == {
        f"fixture:{index}" for index in range(5)
    }

    replacement = LifecycleAdapter("fixture:2")
    await managed.replace("fixture:2", replacement)
    assert children[2].disconnect_count == 1
    assert await managed.detach("fixture:2")
    assert replacement.disconnect_count == 1
    assert len(managed.adapters) == 4
    await managed.disconnect()
