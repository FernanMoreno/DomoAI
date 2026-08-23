"""Composite AdapterPort for multiple independent source adapters."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from itertools import count
from typing import Any, cast

from domoai.domain.models import (
    AdapterDiagnosticEvent,
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
)
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.ports import AdapterPort
from domoai.runtime.registry import DeviceRegistry

DEFAULT_DIAGNOSTICS_MAX_SIZE = 1000
StateEventKey = tuple[str, ...]


class CompositeAdapter:
    """Coordinate adapters without becoming an agent-facing protocol bus."""

    adapter_id = "composite"

    def __init__(
        self,
        adapters: Sequence[AdapterPort],
        *,
        registry: DeviceRegistry | None = None,
        event_queue_max_size: int = 1000,
        diagnostics_max_size: int = DEFAULT_DIAGNOSTICS_MAX_SIZE,
    ) -> None:
        if not adapters:
            raise ValueError("CompositeAdapter requires at least one adapter")
        if event_queue_max_size <= 0:
            raise ValueError("CompositeAdapter event_queue_max_size must be positive")
        if diagnostics_max_size <= 0:
            raise ValueError("CompositeAdapter diagnostics_max_size must be positive")
        adapter_ids = [adapter.adapter_id for adapter in adapters]
        if len(set(adapter_ids)) != len(adapter_ids):
            raise ValueError("CompositeAdapter requires unique adapter IDs")
        self.adapters = tuple(adapters)
        self._by_id = {adapter.adapter_id: adapter for adapter in self.adapters}
        self.registry = registry
        self._connected: set[str] = set()
        self._diagnostics: deque[dict[str, Any]] = deque(maxlen=diagnostics_max_size)
        self._event_queue_max_size = event_queue_max_size
        self._dropped_events_total = 0
        self._dropped_events_by_adapter: defaultdict[str, int] = defaultdict(int)
        self._dropped_events_by_kind: defaultdict[str, int] = defaultdict(int)
        self._coalesced_events_total = 0
        self._bulk_queue: asyncio.Queue[Any] | None = None
        self._priority_queue: asyncio.Queue[Any] | None = None

    @property
    def dropped_events_total(self) -> int:
        return self._dropped_events_total

    @property
    def dropped_events_by_adapter(self) -> dict[str, int]:
        return dict(self._dropped_events_by_adapter)

    @property
    def dropped_events_by_kind(self) -> dict[str, int]:
        return dict(self._dropped_events_by_kind)

    @property
    def coalesced_events_total(self) -> int:
        return self._coalesced_events_total

    @property
    def event_queue_depth(self) -> dict[str, int]:
        return {
            "bulk": self._bulk_queue.qsize() if self._bulk_queue is not None else 0,
            "priority": self._priority_queue.qsize() if self._priority_queue is not None else 0,
        }

    def bind_registry(self, registry: DeviceRegistry) -> None:
        self.registry = registry

    @property
    def diagnostics(self) -> list[dict[str, Any]]:
        return list(self._diagnostics)

    async def connect(self) -> None:
        results = await asyncio.gather(
            *(adapter.connect() for adapter in self.adapters), return_exceptions=True
        )
        self._connected.clear()
        self._diagnostics.clear()
        for adapter, result in zip(self.adapters, results, strict=True):
            if isinstance(result, BaseException):
                self._record_failure(adapter.adapter_id, "adapter_connect_failed", result)
            else:
                self._connected.add(adapter.adapter_id)
        if not self._connected:
            raise ConnectionError("No configured source adapter is available")

    async def disconnect(self) -> None:
        await asyncio.gather(
            *(adapter.disconnect() for adapter in self.adapters), return_exceptions=True
        )
        self._connected.clear()

    async def discover(self) -> AdapterSnapshot:
        candidates = [adapter for adapter in self.adapters if adapter.adapter_id in self._connected]
        results = await asyncio.gather(
            *(adapter.discover() for adapter in candidates), return_exceptions=True
        )
        source_entities: list[dict[str, Any]] = []
        source_states: list[dict[str, Any]] = []
        areas: dict[str, dict[str, Any]] = {}
        unsupported: list[dict[str, Any]] = []
        successful = 0
        for adapter, result in zip(candidates, results, strict=True):
            if isinstance(result, BaseException):
                self._record_failure(adapter.adapter_id, "adapter_discovery_failed", result)
                unsupported.append(
                    {
                        "adapter_id": adapter.adapter_id,
                        "failure": True,
                        "reason": str(result)[:200],
                    }
                )
                continue
            successful += 1
            source_entities.extend(self._annotate_items(result.source_entities, adapter.adapter_id))
            source_states.extend(self._annotate_items(result.source_states, adapter.adapter_id))
            for area in result.areas:
                area_id = str(area.get("id", ""))
                if area_id:
                    areas.setdefault(area_id, dict(area))
            unsupported.extend(self._annotate_items(result.unsupported_sources, adapter.adapter_id))
        if successful == 0:
            raise ConnectionError("All configured source adapters failed discovery")
        return AdapterSnapshot(
            source_entities=source_entities,
            source_states=source_states,
            areas=list(areas.values()),
            unsupported_sources=unsupported,
        )

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        grouped: dict[str, list[SourceRef]] = defaultdict(list)
        for source_ref in source_refs:
            grouped[source_ref.adapter_id].append(source_ref)
        results: list[StateSnapshot] = []
        failures: list[Exception] = []
        for adapter_id, refs in grouped.items():
            adapter = self._by_id.get(adapter_id)
            if adapter is None or adapter_id not in self._connected:
                failures.append(ConnectionError(f"Source adapter {adapter_id!r} is unavailable"))
                continue
            try:
                results.extend(await adapter.read_state(refs))
            except (ConnectionError, OSError, TimeoutError) as error:
                failures.append(ConnectionError(f"Source adapter {adapter_id!r} read failed"))
                self._record_failure(adapter_id, "adapter_read_failed", error)
        if failures and not results:
            raise failures[0]
        return results

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        if self.registry is None:
            return AdapterExecutionAck(
                accepted=False, message="Composite route registry is unavailable"
            )
        resolution = self.registry.resolve_command_route(command.device_id, command.command)
        if resolution.route is None:
            return AdapterExecutionAck(
                accepted=False,
                message=f"Command route rejected: {resolution.reason or 'unknown'}",
            )
        route = resolution.route
        adapter = self._by_id.get(route.source_ref.adapter_id)
        if adapter is None or route.source_ref.adapter_id not in self._connected:
            return AdapterExecutionAck(
                accepted=False, message="Command source adapter is unavailable"
            )
        execute_source = getattr(adapter, "execute_source", None)
        if callable(execute_source):
            source_executor = cast(
                Callable[..., Awaitable[AdapterExecutionAck]], execute_source
            )
            if execution_context is None:
                return await source_executor(command, route.source_ref.external_id)
            return await source_executor(
                command, route.source_ref.external_id, execution_context
            )
        local_command = command.model_copy(update={"device_id": route.local_canonical_id})
        if execution_context is None:
            return await adapter.execute(local_command)
        return await adapter.execute(local_command, execution_context)

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[SourceEvent]:
        # state_changed traffic is high-frequency and self-correcting (the
        # next reading supersedes a dropped one), so it alone is subject to
        # the bounded, drop-on-full queue. Structural events (availability,
        # membership, metadata) are comparatively rare and NOT
        # self-correcting -- losing one can leave the runtime's topology
        # understanding silently wrong indefinitely -- so they, along with
        # stream-error/stream-end signaling, go through a bounded queue whose
        # producers await capacity instead of dropping an item.
        Item = tuple[str, SourceEvent | None, BaseException | None]
        bulk_queue: asyncio.Queue[StateEventKey] = asyncio.Queue(
            maxsize=self._event_queue_max_size
        )
        bulk_pending: dict[StateEventKey, tuple[str, StateChangedEvent]] = {}
        priority_queue: asyncio.Queue[Item] = asyncio.Queue(
            maxsize=self._event_queue_max_size
        )
        self._bulk_queue = bulk_queue
        self._priority_queue = priority_queue
        active = [adapter for adapter in self.adapters if adapter.adapter_id in self._connected]
        sequence = count()

        async def pump(adapter: AdapterPort) -> None:
            cancelled = False
            try:
                async for event in adapter.subscribe_events():
                    if isinstance(event, StateChangedEvent):
                        key = self._state_event_key(adapter.adapter_id, event, next(sequence))
                        if key in bulk_pending:
                            bulk_pending[key] = (adapter.adapter_id, event)
                            self._coalesced_events_total += 1
                        elif len(bulk_pending) < self._event_queue_max_size:
                            bulk_pending[key] = (adapter.adapter_id, event)
                            bulk_queue.put_nowait(key)
                        else:
                            self._record_drop(adapter.adapter_id, event)
                    else:
                        await priority_queue.put((adapter.adapter_id, event, None))
            except asyncio.CancelledError:
                cancelled = True
                raise
            except (ConnectionError, OSError, TimeoutError) as error:
                await priority_queue.put((adapter.adapter_id, None, error))
            finally:
                if not cancelled:
                    await priority_queue.put((adapter.adapter_id, None, None))

        tasks = [asyncio.create_task(pump(adapter)) for adapter in active]
        remaining = len(tasks)
        priority_get: asyncio.Task[Item] = asyncio.ensure_future(priority_queue.get())
        bulk_get: asyncio.Task[StateEventKey] = asyncio.ensure_future(bulk_queue.get())
        try:
            # bulk_get may already hold a resolved item at the moment the
            # last stream-end sentinel (delivered via priority_queue) drops
            # `remaining` to 0 -- keep draining until that item (and any
            # still sitting in the queue) is consumed too, not just until
            # every producer has signalled it is done.
            while remaining or bulk_get.done() or not bulk_queue.empty():
                if priority_get.done():
                    item = priority_get.result()
                    priority_get = asyncio.ensure_future(priority_queue.get())
                elif bulk_get.done():
                    key = bulk_get.result()
                    bulk_get = asyncio.ensure_future(bulk_queue.get())
                    pending = bulk_pending.pop(key, None)
                    if pending is None:
                        continue
                    pending_adapter_id, pending_event = pending
                    item = (pending_adapter_id, pending_event, None)
                else:
                    await asyncio.wait(
                        {priority_get, bulk_get}, return_when=asyncio.FIRST_COMPLETED
                    )
                    continue
                adapter_id, event, error = item
                if error is not None:
                    self._record_failure(adapter_id, "adapter_event_stream_failed", error)
                    yield AdapterDiagnosticEvent(
                        source_adapter_id=adapter_id,
                        payload={
                            "source_adapter_id": adapter_id,
                            "reason": str(error)[:200],
                        },
                    )
                elif event is None:
                    remaining -= 1
                else:
                    payload = dict(event.payload)
                    payload["source_adapter_id"] = adapter_id
                    yield type(event).model_validate(
                        event.model_dump(mode="python")
                        | {"source_adapter_id": adapter_id, "payload": payload}
                    )
        finally:
            priority_get.cancel()
            bulk_get.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def health(self) -> AdapterHealth:
        health = await asyncio.gather(
            *(adapter.health() for adapter in self.adapters), return_exceptions=True
        )
        connected = any(isinstance(item, AdapterHealth) and item.connected for item in health)
        messages = [
            item.message for item in health if isinstance(item, AdapterHealth) and item.message
        ]
        components = [item for item in health if isinstance(item, AdapterHealth)]
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=connected,
            message="; ".join(messages)[:200] if messages else None,
            components=components,
        )

    def _record_drop(self, adapter_id: str, event: SourceEvent) -> None:
        self._dropped_events_total += 1
        self._dropped_events_by_adapter[adapter_id] += 1
        self._dropped_events_by_kind[event.kind] += 1

    @staticmethod
    def _state_event_key(
        adapter_id: str, event: StateChangedEvent, sequence: int
    ) -> StateEventKey:
        payload = event.payload
        identity = CompositeAdapter._extract_state_identity(payload)
        scope = CompositeAdapter._extract_state_scope(payload)
        if identity is None:
            return (adapter_id, "unique", str(sequence))
        return (adapter_id, *identity, "scope", *scope)

    @staticmethod
    def _extract_state_identity(payload: Mapping[str, Any]) -> tuple[str, ...] | None:
        candidates: list[Mapping[str, Any]] = [payload]
        for key in ("event", "data", "new_state"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                candidates.append(nested)
                nested_data = nested.get("data")
                if isinstance(nested_data, Mapping):
                    candidates.append(nested_data)

        for candidate in candidates:
            entity_ids = candidate.get("entity_ids")
            if isinstance(entity_ids, (list, tuple, set)) and entity_ids:
                values = tuple(sorted(str(value) for value in entity_ids))
                return ("entity_ids", *values)
            for field in ("entity_id", "friendly_name", "node_id"):
                value = candidate.get(field)
                if value is not None and str(value):
                    return (field, str(value))
        return None

    @staticmethod
    def _extract_state_scope(payload: Mapping[str, Any]) -> tuple[str, ...]:
        capabilities = payload.get("capabilities")
        if isinstance(capabilities, (list, tuple, set)):
            return tuple(sorted(str(value) for value in capabilities))
        for field in ("capability", "path"):
            value = payload.get(field)
            if value is not None and str(value):
                return (str(value),)
        return ()

    def _record_failure(self, adapter_id: str, kind: str, error: BaseException) -> None:
        if self.registry is not None:
            self.registry.mark_source_unavailable(adapter_id)
        self._diagnostics.append(
            {
                "event_type": kind,
                "adapter_id": adapter_id,
                "message": str(error)[:200],
            }
        )

    @staticmethod
    def _annotate_items(items: list[dict[str, Any]], adapter_id: str) -> list[dict[str, Any]]:
        annotated: list[dict[str, Any]] = []
        for item in items:
            value = dict(item)
            value["source_adapter_id"] = adapter_id
            annotated.append(value)
        return annotated
