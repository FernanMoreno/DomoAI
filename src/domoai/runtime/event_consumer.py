"""Apply adapter events through the shared discovery/state boundary."""

from __future__ import annotations

import asyncio

from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import SourceEvent, SourceRef
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import AdapterPort
from domoai.runtime.state_store import StateStore

_VALUE_ONLY_EVENT_KIND = "state_changed"


class RuntimeEventConsumer:
    """Convert source notifications into refreshed canonical runtime state."""

    def __init__(
        self,
        adapter: AdapterPort,
        discovery: DiscoveryService,
        state_store: StateStore,
        audit: AuditLog,
    ) -> None:
        self.adapter = adapter
        self.discovery = discovery
        self.state_store = state_store
        self.audit = audit

    async def consume_once(self) -> SourceEvent | None:
        """Apply one event, or mark cached state stale when the source is lost."""

        try:
            event = await anext(self.adapter.subscribe_events())
        except StopAsyncIteration:
            return None
        except (ConnectionError, OSError) as error:
            stale = await self.state_store.mark_all_stale()
            self.audit.append(
                event_type="source_event_stream_unavailable",
                actor="runtime",
                subject_id=self.adapter.adapter_id,
                payload={"error": str(error), "stale_states": len(stale)},
            )
            return None

        try:
            await self._apply_event(event)
        except (ConnectionError, OSError) as error:
            stale = await self.state_store.mark_all_stale()
            self.audit.append(
                event_type="source_event_stream_unavailable",
                actor="runtime",
                subject_id=self.adapter.adapter_id,
                payload={"error": str(error), "stale_states": len(stale)},
            )
            return None

        self.audit.append(
            event_type="source_event_applied",
            actor="runtime",
            subject_id=self.adapter.adapter_id,
            payload={"kind": event.kind},
        )
        return event

    async def run(
        self, *, reconnect_delay: float = 1.0, max_reconnect_delay: float = 60.0
    ) -> None:
        """Keep applying events and reconnect (with capped backoff) after a failure."""

        delay = reconnect_delay
        while True:
            health = await self.adapter.health()
            degraded = not health.connected or (
                health.components is not None
                and any(not component.connected for component in health.components)
            )
            if degraded:
                try:
                    await self.adapter.connect()
                    await self.discovery.refresh()
                except (ConnectionError, OSError) as error:
                    await self._mark_unavailable(error)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_reconnect_delay)
                    continue
                delay = reconnect_delay

            try:
                async for event in self.adapter.subscribe_events():
                    try:
                        await self._apply_event(event)
                    except (ConnectionError, OSError) as error:
                        await self._mark_unavailable(error)
                        break
                else:
                    return
            except (ConnectionError, OSError) as error:
                await self._mark_unavailable(error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_reconnect_delay)

    async def _apply_event(self, event: SourceEvent) -> None:
        """Apply state-only events cheaply; fall back to full discovery otherwise."""

        if event.kind == _VALUE_ONLY_EVENT_KIND:
            await self._apply_state_only()
        else:
            await self.discovery.refresh()

    async def _apply_state_only(self) -> None:
        source_refs = self._known_source_refs(self.adapter.adapter_id)
        if not source_refs:
            return
        snapshots = await self.adapter.read_state(source_refs)
        registry = self.discovery.registry
        for snapshot in snapshots:
            canonical_id = registry.canonical_id_for_source(
                snapshot.source_ref.adapter_id, snapshot.source_ref.external_id
            )
            if canonical_id is None:
                continue
            normalized = snapshot.model_copy(update={"device_id": canonical_id})
            await self.state_store.save(normalized)

    def _known_source_refs(self, adapter_id: str) -> list[SourceRef]:
        return [
            source_ref
            for device in self.discovery.registry.devices
            for source_ref in device.source_refs
            if source_ref.adapter_id == adapter_id
        ]

    async def _mark_unavailable(self, error: Exception) -> None:
        stale = await self.state_store.mark_all_stale()
        self.audit.append(
            event_type="source_event_stream_unavailable",
            actor="runtime",
            subject_id=self.adapter.adapter_id,
            payload={"error": str(error), "stale_states": len(stale)},
        )
