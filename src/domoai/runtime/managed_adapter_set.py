"""Dynamically managed runtime adapter set.

``CompositeAdapter`` remains the static compatibility boundary. This class
owns the mutable child lifecycle and delegates routing, inventory, health and
the established queue policy to that boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from domoai.domain.models import (
    AdapterDiagnosticEvent,
    AdapterExecutionAck,
    Command,
    SourceEvent,
    StateChangedEvent,
)
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.ports import AdapterPort


class ManagedAdapterSet(CompositeAdapter):
    """Attach, replace and detach physical children without a process restart."""

    def __init__(
        self,
        adapters: Sequence[AdapterPort] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(adapters, allow_empty=True, **kwargs)
        self._mutation_lock = asyncio.Lock()
        self._child_pump_tasks: dict[str, asyncio.Task[None]] = {}
        self._event_queue: asyncio.Queue[tuple[str, SourceEvent]] | None = None
        self._bulk_pending: dict[tuple[str, ...], tuple[str, StateChangedEvent]] = {}
        self._event_stream_active = False

    async def attach(self, adapter: AdapterPort) -> None:
        """Connect and publish one child atomically."""

        async with self._mutation_lock:
            if adapter.adapter_id in self._by_id:
                raise ValueError("managed adapter IDs must be unique")
            try:
                await adapter.connect()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - boundary sanitizes failures
                self._record_failure(
                    adapter.adapter_id,
                    "adapter_attach_failed",
                    ConnectionError("adapter unavailable"),
                )
                raise ConnectionError("adapter unavailable") from error
            self.adapters = (*self.adapters, adapter)
            self._by_id[adapter.adapter_id] = adapter
            self._connected.add(adapter.adapter_id)
            self._start_child_pump(adapter)

    async def replace(self, adapter_id: str, adapter: AdapterPort) -> None:
        """Replace one child while retaining its stable runtime identity."""

        async with self._mutation_lock:
            current = self._by_id.get(adapter_id)
            if current is None:
                raise KeyError(f"unknown managed adapter {adapter_id!r}")
            if adapter.adapter_id != adapter_id:
                raise ValueError("replacement adapter_id must match the managed identity")
            try:
                await adapter.connect()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - boundary sanitizes failures
                self._record_failure(
                    adapter_id,
                    "adapter_replace_failed",
                    ConnectionError("adapter unavailable"),
                )
                raise ConnectionError("adapter unavailable") from error
            await self._stop_child_pump(adapter_id)
            try:
                await current.disconnect()
            finally:
                self.adapters = tuple(
                    adapter if item.adapter_id == adapter_id else item
                    for item in self.adapters
                )
                self._by_id[adapter_id] = adapter
                self._connected.add(adapter_id)
                self._start_child_pump(adapter)

    async def detach(self, adapter_id: str) -> bool:
        """Cancel event ownership, disconnect and remove a child idempotently."""

        async with self._mutation_lock:
            adapter = self._by_id.get(adapter_id)
            if adapter is None:
                return False
            await self._stop_child_pump(adapter_id)
            try:
                await adapter.disconnect()
            finally:
                self.adapters = tuple(
                    item for item in self.adapters if item.adapter_id != adapter_id
                )
                self._by_id.pop(adapter_id, None)
                self._connected.discard(adapter_id)
                if self.registry is not None:
                    self.registry.mark_source_unavailable(adapter_id)
            return True

    async def connect(self) -> None:
        async with self._mutation_lock:
            await super().connect()
            for adapter in self.adapters:
                if adapter.adapter_id in self._connected:
                    self._start_child_pump(adapter)

    async def disconnect(self) -> None:
        async with self._mutation_lock:
            for adapter_id in tuple(self._child_pump_tasks):
                await self._stop_child_pump(adapter_id)
            await super().disconnect()

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        if not self.adapters:
            return AdapterExecutionAck(
                accepted=False,
                message="Physical source unavailable; no connection is active",
            )
        return await super().execute(command, execution_context)

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[SourceEvent]:
        if self._event_stream_active:
            raise RuntimeError("ManagedAdapterSet supports one event subscriber")
        queue: asyncio.Queue[tuple[str, SourceEvent]] = asyncio.Queue(
            maxsize=self._event_queue_max_size
        )
        self._event_queue = queue
        self._bulk_pending = {}
        self._event_stream_active = True
        for adapter in self.adapters:
            if adapter.adapter_id in self._connected:
                self._start_child_pump(adapter)
        try:
            while True:
                adapter_id, event = await queue.get()
                if isinstance(event, StateChangedEvent):
                    key = self._state_event_key(adapter_id, event, 0)
                    pending = self._bulk_pending.pop(key, None)
                    if pending is not None:
                        event = pending[1]
                payload = dict(event.payload)
                payload["source_adapter_id"] = adapter_id
                yield type(event).model_validate(
                    event.model_dump(mode="python")
                    | {"source_adapter_id": adapter_id, "payload": payload}
                )
        finally:
            self._event_stream_active = False
            self._event_queue = None
            for adapter_id in tuple(self._child_pump_tasks):
                await self._stop_child_pump(adapter_id)

    def _start_child_pump(self, adapter: AdapterPort) -> None:
        if not self._event_stream_active or adapter.adapter_id in self._child_pump_tasks:
            return
        self._child_pump_tasks[adapter.adapter_id] = asyncio.create_task(
            self._pump_child(adapter),
            name=f"managed-adapter:{adapter.adapter_id}",
        )

    async def _stop_child_pump(self, adapter_id: str) -> None:
        task = self._child_pump_tasks.pop(adapter_id, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _pump_child(self, adapter: AdapterPort) -> None:
        delay = self._reconnect_initial_delay
        while True:
            stream_failed = False
            try:
                async for event in adapter.subscribe_events():
                    queue = self._event_queue
                    if queue is None:
                        return
                    if isinstance(event, StateChangedEvent):
                        key = self._state_event_key(adapter.adapter_id, event, 0)
                        if key in self._bulk_pending:
                            self._bulk_pending[key] = (adapter.adapter_id, event)
                            self._coalesced_events_total += 1
                        elif len(self._bulk_pending) < self._event_queue_max_size:
                            self._bulk_pending[key] = (adapter.adapter_id, event)
                            queue.put_nowait((adapter.adapter_id, event))
                        else:
                            self._record_drop(adapter.adapter_id, event)
                    else:
                        await queue.put((adapter.adapter_id, event))
                stream_failed = self._reconnect_on_stream_end
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - child failures are sanitized below
                stream_failed = True
                self._record_failure(
                    adapter.adapter_id,
                    "adapter_event_stream_failed",
                    ConnectionError("adapter unavailable"),
                )
                queue = self._event_queue
                if queue is not None:
                    await queue.put(
                        (
                            adapter.adapter_id,
                            AdapterDiagnosticEvent(
                                source_adapter_id=adapter.adapter_id,
                                code="source_unavailable",
                                message="adapter unavailable",
                                payload={"source_adapter_id": adapter.adapter_id},
                            ),
                        )
                    )
            if not stream_failed:
                return
            while True:
                self._reconnect_attempts_total += 1
                await asyncio.sleep(delay)
                try:
                    await adapter.connect()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - failure is intentionally generic
                    self._reconnect_failure_total += 1
                    delay = min(
                        max(delay * 2, self._reconnect_initial_delay),
                        self._reconnect_max_delay,
                    )
                    continue
                self._connected.add(adapter.adapter_id)
                self._reconnect_success_total += 1
                queue = self._event_queue
                if queue is not None:
                    await queue.put(
                        (
                            adapter.adapter_id,
                            AdapterDiagnosticEvent(
                                source_adapter_id=adapter.adapter_id,
                                code="source_reconnected",
                                message="adapter reconnected",
                                payload={"source_adapter_id": adapter.adapter_id},
                            ),
                        )
                    )
                delay = self._reconnect_initial_delay
                break


__all__ = ["ManagedAdapterSet"]
