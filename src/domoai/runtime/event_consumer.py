"""Apply adapter events through the shared discovery/state boundary."""

from __future__ import annotations

import asyncio
from datetime import datetime

from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import SourceEvent, SourceRef, StateChangedEvent
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import AdapterPort
from domoai.runtime.state_store import StateStore


class RuntimeEventConsumer:
    """Convert source notifications into refreshed canonical runtime state."""

    def __init__(
        self,
        adapter: AdapterPort,
        discovery: DiscoveryService,
        state_store: StateStore,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.adapter = adapter
        self.discovery = discovery
        self.state_store = state_store
        self.audit = audit
        self.clock = clock or SystemClock()
        self.alive = False
        self.events_applied = 0
        self.last_event_at: datetime | None = None
        self.last_event_lag_seconds: float | None = None

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

    async def run(self, *, reconnect_delay: float = 1.0, max_reconnect_delay: float = 60.0) -> None:
        """Keep applying events and reconnect (with capped backoff) after a failure."""

        self.alive = True
        try:
            delay = reconnect_delay
            while True:
                try:
                    health = await self.adapter.health()
                    degraded = not health.connected or (
                        health.components is not None
                        and any(not component.connected for component in health.components)
                    )
                except Exception as error:
                    await self._mark_unavailable(error)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_reconnect_delay)
                    continue

                if degraded:
                    try:
                        await self.adapter.connect()
                        await self.discovery.refresh()
                    except Exception as error:
                        await self._mark_unavailable(error)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, max_reconnect_delay)
                        continue
                    delay = reconnect_delay
                else:
                    delay = reconnect_delay

                try:
                    async for event in self.adapter.subscribe_events():
                        try:
                            await self._apply_event(event)
                        except Exception as error:
                            await self._mark_unavailable(error)
                            break
                    else:
                        await self._mark_stream_ended()
                except Exception as error:
                    await self._mark_unavailable(error)
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_reconnect_delay)
        finally:
            self.alive = False

    async def _apply_event(self, event: SourceEvent) -> None:
        """Apply state-only events cheaply; fall back to full discovery otherwise."""

        self.events_applied += 1
        occurred_at = getattr(event, "occurred_at", None)
        self.last_event_at = occurred_at
        self.last_event_lag_seconds = (
            max(0.0, (self.clock.now() - occurred_at).total_seconds())
            if occurred_at is not None
            else None
        )

        if isinstance(event, StateChangedEvent):
            await self._apply_state_only(event)
        else:
            await self.discovery.refresh()

    async def _apply_state_only(self, event: StateChangedEvent) -> None:
        source_adapter_id = event.source_adapter_id or event.payload.get(
            "source_adapter_id", self.adapter.adapter_id
        )
        source_refs = self._known_source_refs(source_adapter_id)
        if event.external_id is not None:
            source_refs = [
                source_ref
                for source_ref in source_refs
                if source_ref.external_id == event.external_id
            ]
        if not source_refs:
            return
        snapshots = await self.adapter.read_state(source_refs)
        if event.capability is not None:
            snapshots = [
                snapshot for snapshot in snapshots if snapshot.capability == event.capability
            ]
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
        await self._mark_source_unavailable(
            event_type="source_event_stream_unavailable", error=error
        )

    async def _mark_stream_ended(self) -> None:
        await self._mark_source_unavailable(
            event_type="source_event_stream_ended",
            error=ConnectionError("Adapter event stream ended normally"),
        )

    async def _mark_source_unavailable(self, *, event_type: str, error: Exception) -> None:
        try:
            stale = await self.state_store.mark_all_stale()
        except Exception as stale_error:
            stale = []
            error = RuntimeError(f"{error}; stale-state marking failed: {stale_error}")
        payload = {
            "error": str(error)[:200],
            "stale_states": len(stale),
        }
        if event_type == "source_event_stream_ended":
            payload["reason"] = "stream_completed"
        self.audit.append(
            event_type=event_type,
            actor="runtime",
            subject_id=self.adapter.adapter_id,
            payload=payload,
        )
