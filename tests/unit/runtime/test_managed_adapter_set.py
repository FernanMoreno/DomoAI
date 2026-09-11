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
    assert (
        len(
            [
                task
                for task in asyncio.all_tasks()
                if not task.done() and task.get_name().startswith("managed-adapter:fixture:a")
            ]
        )
        == 1
    )

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


@pytest.mark.asyncio
@pytest.mark.parametrize("old_disconnected", [False, True])
async def test_cancelled_replacement_does_not_publish_new_child_or_claim_old_health(
    old_disconnected: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class InterruptedDisconnect(FakeAdapter):
        async def disconnect(self) -> None:
            if old_disconnected:
                await super().disconnect()
            entered.set()
            await release.wait()
            await super().disconnect()

    original = InterruptedDisconnect("fixture:cancel")
    replacement = FakeAdapter("fixture:cancel")
    managed = ManagedAdapterSet([original])
    await managed.connect()
    task = asyncio.create_task(managed.replace(original.adapter_id, replacement))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert replacement.connected
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(child is not replacement for child in managed.adapters)
        assert not replacement.connected
        assert not (await managed.health()).connected
        assert not managed._mutation_lock.locked()
        assert original.connected is (not old_disconnected)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await managed.disconnect()


@pytest.mark.asyncio
async def test_repeated_cancel_drains_only_unpublished_child_cleanup_before_unlock() -> None:
    old_disconnect = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class Old(FakeAdapter):
        async def disconnect(self) -> None:
            old_disconnect.set()
            await asyncio.Event().wait()

    class New(FakeAdapter):
        async def disconnect(self) -> None:
            cleanup_started.set()
            await cleanup_release.wait()
            await super().disconnect()

    original = Old("fixture:cancel")
    replacement = New("fixture:cancel")
    managed = ManagedAdapterSet([original])
    await managed.connect()
    task = asyncio.create_task(managed.replace(original.adapter_id, replacement))
    try:
        await asyncio.wait_for(old_disconnect.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and managed._mutation_lock.locked()
        assert managed.adapters == (original,)
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not replacement.connected and original.connected
        assert replacement.disconnect_count == 1
        assert not managed._mutation_lock.locked()
    finally:
        cleanup_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await FakeAdapter.disconnect(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", ["shutdown", "replacement"])
async def test_failed_cleanup_keeps_unpublished_child_owned_for_explicit_retry(retry: str) -> None:
    entered = asyncio.Event()

    class Old(FakeAdapter):
        async def disconnect(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    class New(FakeAdapter):
        fail = True
        attempts = 0

        async def disconnect(self) -> None:
            self.attempts += 1
            if self.fail:
                raise ConnectionError("synthetic cleanup unavailable")
            await super().disconnect()

    original = Old("fixture:cleanup")
    unpublished = New("fixture:cleanup")
    managed = ManagedAdapterSet([original])
    await managed.connect()
    task = asyncio.create_task(managed.replace(original.adapter_id, unpublished))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert unpublished.connected and unpublished.attempts == 1
        assert managed.adapters == (original,)
        original.disconnect = FakeAdapter.disconnect.__get__(original)
        if retry == "replacement":
            next_child = FakeAdapter(original.adapter_id)
            with pytest.raises(ConnectionError, match="cleanup"):
                await managed.replace(original.adapter_id, next_child)
            assert not next_child.connected
            unpublished.fail = False
            await managed.replace(original.adapter_id, next_child)
            assert managed.adapters == (next_child,) and next_child.connected
        else:
            unpublished.fail = False
            await managed.disconnect()
        assert not unpublished.connected
        assert unpublished.attempts >= 2
    finally:
        unpublished.fail = False
        original.disconnect = FakeAdapter.disconnect.__get__(original)
        await asyncio.gather(task, return_exceptions=True)
        await managed.disconnect()
        await unpublished.disconnect()


@pytest.mark.asyncio
async def test_cancelling_explicit_cleanup_retry_cannot_continue_to_publish_another_child() -> None:
    entered, cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Old(FakeAdapter):
        async def disconnect(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    class New(FakeAdapter):
        fail = True

        async def disconnect(self) -> None:
            if self.fail:
                raise ConnectionError("synthetic cleanup unavailable")
            cleanup_started.set()
            await cleanup_release.wait()
            await super().disconnect()

    original = Old("fixture:retry")
    unpublished = New("fixture:retry")
    next_child = FakeAdapter("fixture:retry")
    managed = ManagedAdapterSet([original])
    await managed.connect()
    task = asyncio.create_task(managed.replace(original.adapter_id, unpublished))
    retry = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        original.disconnect = FakeAdapter.disconnect.__get__(original)
        unpublished.fail = False
        retry = asyncio.create_task(managed.replace(original.adapter_id, next_child))
        await asyncio.wait_for(cleanup_started.wait(), 1)
        retry.cancel()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(retry, 1)
        assert not next_child.connected
        assert managed.adapters == (original,)
        assert not unpublished.connected
    finally:
        unpublished.fail = False
        cleanup_release.set()
        original.disconnect = FakeAdapter.disconnect.__get__(original)
        await asyncio.gather(task, *([retry] if retry else []), return_exceptions=True)
        await managed.disconnect()
        await unpublished.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
@pytest.mark.parametrize("retry", ["attach", "shutdown"])
async def test_cancelled_attach_keeps_acquired_child_reachable_until_cleanup_confirmed(
    cleanup_failure: bool, retry: str,
) -> None:
    acquired = asyncio.Event()

    class InterruptedConnect(FakeAdapter):
        fail_cleanup = cleanup_failure
        cleanup_attempts = 0

        async def connect(self) -> None:
            await super().connect()
            acquired.set()
            await asyncio.Event().wait()

        async def disconnect(self) -> None:
            self.cleanup_attempts += 1
            if self.fail_cleanup:
                raise ConnectionError("synthetic cleanup unavailable")
            await super().disconnect()

    managed = ManagedAdapterSet()
    child = InterruptedConnect("fixture:attach-cancel")
    task = asyncio.create_task(managed.attach(child))
    try:
        await asyncio.wait_for(acquired.wait(), 1)
        assert child.connected
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert managed.adapters == ()
        assert not managed._mutation_lock.locked()
        assert child.cleanup_attempts == 1
        if cleanup_failure:
            assert child.connected
            assert any(owned is child for owned in managed._unpublished_cleanup.values())
            if retry == "attach":
                replacement = FakeAdapter(child.adapter_id)
                with pytest.raises(ConnectionError, match="cleanup"):
                    await managed.attach(replacement)
                assert not replacement.connected
            child.fail_cleanup = False
        if retry == "attach":
            replacement = FakeAdapter(child.adapter_id)
            await managed.attach(replacement)
            assert managed.adapters == (replacement,)
        await managed.disconnect()
        assert not child.connected
        assert not managed._unpublished_cleanup
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        child.fail_cleanup = False
        await managed.disconnect()
        await child.disconnect()


@pytest.mark.asyncio
async def test_cancelled_attach_before_acquisition_stays_unpublished() -> None:
    entered = asyncio.Event()

    class PausedBeforeConnect(FakeAdapter):
        async def connect(self) -> None:
            entered.set()
            await asyncio.Event().wait()
            await super().connect()

    managed = ManagedAdapterSet()
    child = PausedBeforeConnect("fixture:before-acquisition")
    task = asyncio.create_task(managed.attach(child))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert not child.connected
    assert managed.adapters == ()
    await managed.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["attach", "replace"])
@pytest.mark.parametrize("cleanup_failure", [None, "error", "cancel"])
async def test_failed_connect_preserves_primary_error_and_unpublished_cleanup_ownership(
    operation: str, cleanup_failure: str | None
) -> None:
    primary_error = ConnectionError("synthetic primary connect failure")

    class PartiallyAcquired(FakeAdapter):
        fail_cleanup = cleanup_failure
        cleanup_attempts = 0

        async def connect(self) -> None:
            await super().connect()
            raise primary_error

        async def disconnect(self) -> None:
            self.cleanup_attempts += 1
            if self.fail_cleanup == "error":
                raise ConnectionError("synthetic cleanup failure")
            if self.fail_cleanup == "cancel":
                raise asyncio.CancelledError
            await super().disconnect()

    managed = ManagedAdapterSet()
    original = FakeAdapter("fixture:failed-connect")
    child = PartiallyAcquired(original.adapter_id)
    if operation == "replace":
        await managed.attach(original)
    try:
        with pytest.raises(ConnectionError, match="adapter unavailable") as failure:
            if operation == "attach":
                await managed.attach(child)
            else:
                await managed.replace(original.adapter_id, child)
        assert failure.value.__cause__ is primary_error
        assert child.cleanup_attempts == 1
        assert managed.adapters == ((original,) if operation == "replace" else ())
        if operation == "replace":
            assert original.connected and original.disconnect_count == 0
        if cleanup_failure:
            assert child.connected
            assert managed._unpublished_cleanup[id(child)] is child
            assert any(
                item["event_type"] == "adapter_cleanup_failed" for item in managed.diagnostics
            )
            assert "synthetic cleanup failure" not in str(managed.diagnostics)
            child.fail_cleanup = None
        else:
            assert not child.connected and not managed._unpublished_cleanup
        await managed.disconnect()
        assert not child.connected and not managed._unpublished_cleanup
    finally:
        child.fail_cleanup = None
        await managed.disconnect()
        await child.disconnect()
