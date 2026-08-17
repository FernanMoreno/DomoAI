"""Discovery use case shared by MCP and non-MCP callers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from domoai.domain.models import (
    AdapterSnapshot,
    Area,
    Device,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import AdapterPort
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@dataclass(frozen=True)
class DiscoveryResult:
    devices: tuple[Device, ...]
    areas: tuple[Area, ...]
    states: tuple[StateSnapshot, ...]
    runtime_revision: str


class DiscoveryService:
    def __init__(
        self,
        adapter: AdapterPort,
        registry: DeviceRegistry,
        state_store: StateStore,
        audit: AuditLog,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.state_store = state_store
        self.audit = audit

    async def refresh(self) -> DiscoveryResult:
        snapshot = await self.adapter.discover()
        self.state_store.begin_revision()
        for diagnostic in snapshot.unsupported_sources:
            if diagnostic.get("failure"):
                self.registry.mark_source_unavailable(
                    str(diagnostic.get("adapter_id", self.adapter.adapter_id))
                )
        devices, areas = self.registry.apply_snapshot(snapshot, self.adapter.adapter_id)
        states = await self._record_states(snapshot)
        for diagnostic in snapshot.unsupported_sources:
            self.audit.append(
                event_type=str(diagnostic.get("event_type", "adapter_discovery_failed")),
                actor="runtime",
                subject_id=str(diagnostic.get("adapter_id", self.adapter.adapter_id)),
                payload={"reason": str(diagnostic.get("reason", "source unavailable"))[:200]},
            )
        revision = self.state_store.runtime_revision
        self.audit.append(
            event_type="discovery_completed",
            actor="runtime",
            subject_id=self.adapter.adapter_id,
            payload={
                "devices": len(devices),
                "areas": len(areas),
                "states": len(states),
                "revision": revision,
            },
        )
        return DiscoveryResult(tuple(devices), tuple(areas), tuple(states), revision)

    async def _record_states(self, snapshot: AdapterSnapshot) -> list[StateSnapshot]:
        received_at = datetime.now(UTC)
        states: list[StateSnapshot] = []
        for raw_state in snapshot.source_states:
            external_id = str(raw_state["entity_id"])
            source_adapter_id = str(raw_state.get("source_adapter_id", self.adapter.adapter_id))
            device_id = self.registry.canonical_id_for_source(source_adapter_id, external_id)
            if device_id is None:
                continue
            status = (
                StateStatus.CURRENT if raw_state.get("available", True) else StateStatus.UNAVAILABLE
            )
            state = StateSnapshot(
                device_id=device_id,
                capability=str(raw_state["capability"]),
                value=raw_state.get("value"),
                unit=raw_state.get("unit"),
                observed_at=received_at,
                received_at=received_at,
                status=status,
                source_ref=SourceRef(
                    adapter_id=source_adapter_id,
                    external_id=external_id,
                ),
            )
            await self.state_store.save(state)
            states.append(state)
        return states
