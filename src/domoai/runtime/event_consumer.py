"""Apply adapter events through the shared discovery/state boundary."""

from __future__ import annotations

import asyncio

from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import SourceEvent
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
            await self.discovery.refresh()
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

    async def run(self, *, reconnect_delay: float = 1.0) -> None:
        """Keep applying events and reconnect after a source stream failure."""

        while True:
            try:
                async for _event in self.adapter.subscribe_events():
                    try:
                        await self.discovery.refresh()
                    except (ConnectionError, OSError) as error:
                        await self._mark_unavailable(error)
                        break
                else:
                    return
            except (ConnectionError, OSError) as error:
                await self._mark_unavailable(error)
            await asyncio.sleep(reconnect_delay)

    async def _mark_unavailable(self, error: Exception) -> None:
        stale = await self.state_store.mark_all_stale()
        self.audit.append(
            event_type="source_event_stream_unavailable",
            actor="runtime",
            subject_id=self.adapter.adapter_id,
            payload={"error": str(error), "stale_states": len(stale)},
        )
