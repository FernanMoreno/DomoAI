"""Apply adapter events through the shared discovery/state boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime

from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import (
    AdapterDiagnosticEvent,
    AvailabilityChangedEvent,
    DeviceMembershipChangedEvent,
    MetadataChangedEvent,
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
)
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
            stale = await self.discovery.apply_source_availability(
                self.adapter.adapter_id, available=False
            )
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
            stale = await self.discovery.apply_source_availability(
                self.adapter.adapter_id, available=False
            )
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
        """Apply events at the narrowest safe state/inventory boundary."""

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
        elif isinstance(event, AvailabilityChangedEvent):
            await self._apply_availability(event)
        elif isinstance(event, (DeviceMembershipChangedEvent, MetadataChangedEvent)):
            # These events change the executable inventory or its semantic
            # metadata.  They are the only event kinds that justify a full
            # discovery; state/transport diagnostics must not turn into a
            # repeated read of every device.
            await self.discovery.refresh()
        elif isinstance(event, AdapterDiagnosticEvent) and event.code == "source_unavailable":
            source_adapter_id = event.source_adapter_id or self.adapter.adapter_id
            await self.discovery.apply_source_availability(
                str(source_adapter_id), external_id=event.external_id, available=False
            )
        elif isinstance(event, AdapterDiagnosticEvent):
            # Diagnostics describe an observation problem, not a topology
            # change.  Keep the last canonical evidence and let the source
            # health/refresh paths mark it unavailable when appropriate.
            return
        else:
            # SourceEvent is a closed union, so this is defensive only for a
            # future model extension.  Do not make an unknown event a broad
            # physical read by default.
            return

    async def _apply_availability(self, event: AvailabilityChangedEvent) -> None:
        """Apply transport/entity availability without rebuilding inventory."""

        source_adapter_id = event.source_adapter_id or event.payload.get(
            "source_adapter_id", self.adapter.adapter_id
        )
        await self.discovery.apply_source_availability(
            str(source_adapter_id),
            external_id=event.external_id,
            available=event.available is True,
        )

    async def _apply_state_only(self, event: StateChangedEvent) -> None:
        source_adapter_id = event.source_adapter_id or event.payload.get(
            "source_adapter_id", self.adapter.adapter_id
        )
        try:
            embedded_snapshots = self._event_snapshots(event, str(source_adapter_id))
        except (TypeError, ValueError) as error:
            # Invalid source evidence must not be repaired by asking the same
            # physical source to answer another read. Mark that source
            # unavailable and keep the runtime fail-closed.
            await self.discovery.apply_source_availability(
                str(source_adapter_id), available=False
            )
            self.audit.append(
                event_type="source_event_state_invalid",
                actor="runtime",
                subject_id=str(source_adapter_id),
                payload={"error": str(error)[:200]},
            )
            return
        if embedded_snapshots is not None:
            await self.discovery.save_state_snapshots(embedded_snapshots)
            return

        # An authoritative event stream already contains the source's
        # observation. Never turn a missing/legacy event payload into a
        # physical read: for KNX that would create GroupValueRead -> response
        # -> StateChangedEvent recursion. A malformed event is handled as
        # missing evidence and remains fail-closed.
        if str(source_adapter_id) in self._event_driven_source_ids():
            self.audit.append(
                event_type="source_event_state_missing",
                actor="runtime",
                subject_id=str(source_adapter_id),
                payload={"reason": "authoritative event did not carry state evidence"},
            )
            return

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
        await self.discovery.save_state_snapshots(snapshots)

    def _event_driven_source_ids(self) -> frozenset[str]:
        declared = getattr(self.adapter, "event_driven_state_adapter_ids", None)
        if declared is None and getattr(self.adapter, "state_events_are_authoritative", False):
            declared = {self.adapter.adapter_id}
        return frozenset(str(adapter_id) for adapter_id in (declared or ()))

    def _event_snapshots(
        self, event: StateChangedEvent, source_adapter_id: str
    ) -> list[StateSnapshot] | None:
        """Decode source-owned state evidence without performing adapter I/O.

        ``None`` means this is a legacy event with no embedded evidence. An
        empty list is a valid event with no known canonical routes; in both
        cases the caller must not manufacture a read for an authoritative
        source.
        """

        raw_states = event.payload.get("states")
        if raw_states is None:
            return None
        if not isinstance(raw_states, list):
            raise ValueError("state event evidence must be a list")

        snapshots: list[StateSnapshot] = []
        for raw_state in raw_states:
            if not isinstance(raw_state, Mapping):
                raise ValueError("state event evidence entries must be objects")
            snapshot = StateSnapshot.model_validate(raw_state)
            if snapshot.source_ref.adapter_id != source_adapter_id:
                raise ValueError("state event source adapter does not match evidence")
            canonical_id = self.discovery.registry.canonical_id_for_source(
                source_adapter_id, snapshot.source_ref.external_id
            )
            if canonical_id is None:
                continue
            snapshots.append(snapshot.model_copy(update={"device_id": canonical_id}))
        return snapshots

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
            source_ids = [self.adapter.adapter_id]
            source_ids.extend(
                str(child.adapter_id)
                for child in getattr(self.adapter, "adapters", ())
                if str(child.adapter_id) not in source_ids
            )
            stale: list[StateSnapshot] = []
            for source_id in source_ids:
                stale.extend(
                    await self.discovery.apply_source_availability(
                        source_id, available=False
                    )
                )
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
