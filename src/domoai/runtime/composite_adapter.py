"""Composite AdapterPort for multiple independent source adapters."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, cast

from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
)
from domoai.runtime.ports import AdapterPort
from domoai.runtime.registry import DeviceRegistry


class CompositeAdapter:
    """Coordinate adapters without becoming an agent-facing protocol bus."""

    adapter_id = "composite"

    def __init__(
        self,
        adapters: Sequence[AdapterPort],
        *,
        registry: DeviceRegistry | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("CompositeAdapter requires at least one adapter")
        adapter_ids = [adapter.adapter_id for adapter in adapters]
        if len(set(adapter_ids)) != len(adapter_ids):
            raise ValueError("CompositeAdapter requires unique adapter IDs")
        self.adapters = tuple(adapters)
        self._by_id = {adapter.adapter_id: adapter for adapter in self.adapters}
        self.registry = registry
        self._connected: set[str] = set()
        self._diagnostics: list[dict[str, Any]] = []

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
            unsupported.extend(
                self._annotate_items(result.unsupported_sources, adapter.adapter_id)
            )
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

    async def execute(self, command: Command) -> AdapterExecutionAck:
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
                Callable[[Command, str], Awaitable[AdapterExecutionAck]], execute_source
            )
            return await source_executor(command, route.source_ref.external_id)
        local_command = command.model_copy(update={"device_id": route.local_canonical_id})
        return await adapter.execute(local_command)

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[SourceEvent]:
        queue: asyncio.Queue[tuple[str, SourceEvent | None, BaseException | None]] = asyncio.Queue()
        active = [adapter for adapter in self.adapters if adapter.adapter_id in self._connected]

        async def pump(adapter: AdapterPort) -> None:
            try:
                async for event in adapter.subscribe_events():
                    await queue.put((adapter.adapter_id, event, None))
            except (ConnectionError, OSError, TimeoutError) as error:
                await queue.put((adapter.adapter_id, None, error))
            finally:
                await queue.put((adapter.adapter_id, None, None))

        tasks = [asyncio.create_task(pump(adapter)) for adapter in active]
        remaining = len(tasks)
        try:
            while remaining:
                adapter_id, event, error = await queue.get()
                if error is not None:
                    self._record_failure(adapter_id, "adapter_event_stream_failed", error)
                    yield SourceEvent(
                        kind="adapter_diagnostic",
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
                    yield SourceEvent(kind=event.kind, payload=payload)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def health(self) -> AdapterHealth:
        health = await asyncio.gather(
            *(adapter.health() for adapter in self.adapters), return_exceptions=True
        )
        connected = any(
            isinstance(item, AdapterHealth) and item.connected for item in health
        )
        messages = [
            item.message
            for item in health
            if isinstance(item, AdapterHealth) and item.message
        ]
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=connected,
            message="; ".join(messages)[:200] if messages else None,
        )

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
