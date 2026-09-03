"""Periodic runtime state refresh owned by the application lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime

from domoai.application.discovery_service import DiscoveryResult, DiscoveryService
from domoai.domain.models import StateSnapshot
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import AdapterPort
from domoai.runtime.state_store import StateStore


class RuntimeStateRefresher:
    """Keep canonical state receipt evidence alive without synthetic writes."""

    def __init__(
        self,
        discovery: DiscoveryService,
        state_store: StateStore,
        audit: AuditLog,
        *,
        interval_seconds: float,
        inventory_refresh_interval_seconds: float | None = None,
        adapter: AdapterPort | None = None,
        clock: Clock | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("state refresh interval must be positive")
        if (
            inventory_refresh_interval_seconds is not None
            and inventory_refresh_interval_seconds <= 0
        ):
            raise ValueError("inventory refresh interval must be positive")
        self.discovery = discovery
        self.state_store = state_store
        self.audit = audit
        self.interval_seconds = interval_seconds
        self.inventory_refresh_interval_seconds = (
            inventory_refresh_interval_seconds or interval_seconds
        )
        self.adapter = adapter or discovery.adapter
        declared_event_sources = getattr(
            self.adapter, "event_driven_state_adapter_ids", None
        )
        if declared_event_sources is None and getattr(
            self.adapter, "state_events_are_authoritative", False
        ):
            declared_event_sources = {self.adapter.adapter_id}
        self.event_driven_state_adapter_ids = frozenset(declared_event_sources or ())
        self.static_inventory_adapter_ids = frozenset(
            getattr(self.adapter, "static_inventory_adapter_ids", ())
        )
        configured_children = getattr(self.adapter, "adapters", None)
        if configured_children is None:
            self.configured_adapter_ids = frozenset({self.adapter.adapter_id})
        else:
            self.configured_adapter_ids = frozenset(
                str(child.adapter_id) for child in configured_children
            )
        self.clock = clock or state_store.clock or SystemClock()
        self.alive = False
        self.refreshes = 0
        self.last_refresh_at: datetime | None = None
        # Runtime startup performs an authoritative discovery before this
        # runner is started. Use that point as the baseline so the first
        # cycle refreshes state promptly without duplicating startup I/O.
        self.last_inventory_refresh_at: datetime = self.clock.now()
        self.last_error: str | None = None

    async def refresh_once(self) -> tuple[object, ...]:
        """Perform one source read and record failures as unavailable evidence."""

        states: tuple[object, ...] = ()
        try:
            if self._inventory_refresh_due():
                result = await self._refresh_inventory()
                if result is not None:
                    states = tuple(result.states)
                states = (*states, *await self._refresh_static_state())
                self.last_inventory_refresh_at = self.clock.now()
            else:
                states = await self._refresh_polled_state()
            await self._mark_disconnected_sources()
        except Exception as error:  # noqa: BLE001 - refresh must not kill the runtime
            self.last_error = str(error)[:200]
            await self._mark_sources_unavailable(error)
        else:
            self.last_error = None
            self.refreshes += 1
        self.last_refresh_at = self.clock.now()
        return states

    def _inventory_refresh_due(self) -> bool:
        age = (self.clock.now() - self.last_inventory_refresh_at).total_seconds()
        return age >= self.inventory_refresh_interval_seconds

    async def _refresh_polled_state(self) -> tuple[StateSnapshot, ...]:
        if self.event_driven_state_adapter_ids:
            return await self.discovery.refresh_state(
                exclude_adapter_ids=self.event_driven_state_adapter_ids
            )
        return await self.discovery.refresh_state()

    async def _refresh_static_state(self) -> tuple[StateSnapshot, ...]:
        """Refresh static sources that still need real freshness evidence."""

        pollable_static_ids = (
            self.static_inventory_adapter_ids - self.event_driven_state_adapter_ids
        )
        if not pollable_static_ids:
            return ()
        excluded_adapter_ids = self.configured_adapter_ids - pollable_static_ids
        return await self.discovery.refresh_state(
            exclude_adapter_ids=excluded_adapter_ids
        )

    async def _refresh_inventory(self) -> DiscoveryResult | None:
        if self.static_inventory_adapter_ids:
            dynamic_ids = self.configured_adapter_ids - self.static_inventory_adapter_ids
            if not dynamic_ids:
                # There is no dynamic inventory to reconcile. Static sources
                # are refreshed separately for state evidence below; do not
                # issue a periodic physical inventory scan.
                return None
            return await self.discovery.refresh(
                exclude_adapter_ids=self.static_inventory_adapter_ids
            )
        return await self.discovery.refresh()

    async def _mark_disconnected_sources(self) -> None:
        """Project partial composite health loss into source-owned state."""

        try:
            health = await self.adapter.health()
        except Exception:
            return
        if health.components is None:
            source_ids = [self.adapter.adapter_id] if not health.connected else []
        else:
            source_ids = [
                component.adapter_id
                for component in health.components
                if not component.connected
            ]
        changed = []
        for source_id in dict.fromkeys(source_ids):
            changed.extend(await self._mark_source_unavailable(source_id))
        if changed:
            self.audit.append(
                event_type="runtime_state_source_unavailable",
                actor="runtime",
                subject_id=self.adapter.adapter_id,
                payload={
                    "source_ids": list(dict.fromkeys(source_ids)),
                    "unavailable_states": len(changed),
                },
            )

    async def run(self) -> None:
        """Refresh immediately, then continue at the bounded configured cadence."""

        self.alive = True
        try:
            while True:
                await self.refresh_once()
                await asyncio.sleep(self.interval_seconds)
        finally:
            self.alive = False

    async def _mark_sources_unavailable(self, error: Exception) -> None:
        source_ids = [self.adapter.adapter_id]
        try:
            health = await self.adapter.health()
            if health.components is not None:
                source_ids.extend(
                    component.adapter_id
                    for component in health.components
                    if not component.connected and component.adapter_id not in source_ids
                )
        except Exception:
            pass
        changed = []
        try:
            for source_id in source_ids:
                changed.extend(await self._mark_source_unavailable(source_id))
        except Exception as state_error:  # noqa: BLE001 - preserve diagnostic context
            error = RuntimeError(f"{error}; unavailable marking failed: {state_error}")
        self.audit.append(
            event_type="runtime_state_refresh_failed",
            actor="runtime",
            subject_id=self.adapter.adapter_id,
            payload={"error": str(error)[:200], "unavailable_states": len(changed)},
        )

    async def _mark_source_unavailable(self, source_id: str) -> list[StateSnapshot]:
        """Use the shared availability boundary, with a test-double fallback."""

        apply_availability = getattr(self.discovery, "apply_source_availability", None)
        if callable(apply_availability):
            return list(await apply_availability(source_id, available=False))
        # Small discovery doubles used by focused refresher tests predate the
        # shared boundary.  Preserve their state-only behavior without making
        # the production path duplicate registry/revision handling.
        registry = getattr(self.discovery, "registry", None)
        if registry is not None:
            registry.mark_source_unavailable(source_id)
        return await self.state_store.mark_source_unavailable(source_id)


__all__ = ["RuntimeStateRefresher"]
