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


class FakeAdapter:
    def __init__(self, adapter_id: str, *, fail_connect: bool = False) -> None:
        self.adapter_id = adapter_id
        self.fail_connect = fail_connect
        self.connected = False
        self.disconnect_count = 0
        self.events: asyncio.Queue[SourceEvent] = asyncio.Queue()

    async def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError("fake transport failed")
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
        return AdapterExecutionAck(accepted=False, message="fake")

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self.connected)

    async def _events(self) -> AsyncIterator[SourceEvent]:
        while self.connected:
            yield await self.events.get()

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        return self._events()


@pytest.mark.asyncio
async def test_empty_set_connects_and_supports_dynamic_attach() -> None:
    managed = ManagedAdapterSet()
    await managed.connect()

    assert managed.adapters == ()
    assert (await managed.discover()).source_entities == []

    adapter = FakeAdapter("fixture:a")
    await managed.attach(adapter)
    assert managed.adapters == (adapter,)
    assert adapter.connected
    await managed.disconnect()
    assert not adapter.connected


@pytest.mark.asyncio
async def test_replace_and_detach_disconnect_children_and_cancel_pumps() -> None:
    first = FakeAdapter("fixture:a")
    second = FakeAdapter("fixture:a")
    managed = ManagedAdapterSet([first], reconnect_initial_delay=0.01, reconnect_max_delay=0.02)
    await managed.connect()

    stream = managed.subscribe_events()
    consumer = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await first.events.put(
        StateChangedEvent(
            source_adapter_id="fixture:a",
            payload={"entity_id": "light.a", "capability": "power", "value": True},
        )
    )
    event = await asyncio.wait_for(consumer, timeout=1)
    assert event.source_adapter_id == "fixture:a"

    await managed.replace("fixture:a", second)
    assert first.disconnect_count == 1
    assert second.connected
    assert len([
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("managed-adapter:fixture:a")
    ]) == 1

    assert await managed.detach("fixture:a")
    assert second.disconnect_count == 1
    assert managed.adapters == ()
    assert not await managed.detach("fixture:a")
    await stream.aclose()


@pytest.mark.asyncio
async def test_attach_is_serialized_and_duplicate_ids_are_rejected() -> None:
    managed = ManagedAdapterSet()
    first = FakeAdapter("fixture:same")
    duplicate = FakeAdapter("fixture:same")

    await managed.attach(first)
    with pytest.raises(ValueError, match="unique"):
        await managed.attach(duplicate)
    assert duplicate.disconnect_count == 0


@pytest.mark.asyncio
async def test_failed_attach_does_not_add_child_or_expose_secret_details() -> None:
    managed = ManagedAdapterSet()
    failing = FakeAdapter("fixture:failed", fail_connect=True)

    with pytest.raises(ConnectionError, match="adapter unavailable"):
        await managed.attach(failing)
    assert managed.adapters == ()
    assert "fake transport" not in "".join(str(item) for item in managed.diagnostics)
