"""Discovery use case shared by MCP and non-MCP callers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
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
from domoai.persistence.repositories import (
    DeviceRepository,
    RuntimeStateMetadataRepository,
    StateSnapshotRepository,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executable_fingerprint import inventory_fingerprint
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
        *,
        device_repository: DeviceRepository | None = None,
        state_snapshot_repository: StateSnapshotRepository | None = None,
        runtime_state_metadata_repository: RuntimeStateMetadataRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.state_store = state_store
        self.audit = audit
        self.device_repository = device_repository
        self.state_snapshot_repository = state_snapshot_repository
        self.runtime_state_metadata_repository = runtime_state_metadata_repository
        # Keep discovery timestamps on the same server-owned clock as the
        # state store.  This matters for deterministic validation and avoids
        # making an observation appear newer merely because the two services
        # were constructed with different clocks.
        self.clock = clock or state_store.clock or SystemClock()
        self._refresh_lock = asyncio.Lock()

    async def refresh(self) -> DiscoveryResult:
        async with self._refresh_lock:
            return await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> DiscoveryResult:
        snapshot = await self.adapter.discover()
        fingerprint_before = self.state_store.inventory_fingerprint or self._inventory_fingerprint()
        for diagnostic in snapshot.unsupported_sources:
            if diagnostic.get("failure"):
                self.registry.mark_source_unavailable(
                    str(diagnostic.get("adapter_id", self.adapter.adapter_id))
                )
        devices, areas = self.registry.apply_snapshot(
            snapshot,
            self.adapter.adapter_id,
            configured_adapter_ids=self._configured_adapter_ids(),
        )
        for diagnostic in self.registry.drain_diagnostics():
            self.audit.append(
                event_type="registry_identity_conflict",
                actor="runtime",
                subject_id=str(
                    diagnostic.get("device_id")
                    or diagnostic.get("adapter_id")
                    or self.adapter.adapter_id
                ),
                payload=diagnostic,
            )
        for device in devices:
            await self.state_store.prune_capabilities(
                device.id, {capability.name for capability in device.capabilities}
            )
        fingerprint_after = self._inventory_fingerprint()
        if fingerprint_after != fingerprint_before:
            self.state_store.begin_revision()
        self.state_store.record_inventory_fingerprint(fingerprint_after)
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
        if self.device_repository is not None and self.state_snapshot_repository is not None:
            current_ids = {device.id for device in self.registry.devices}
            persisted_ids = {device.id for device in await self.device_repository.list_all()}
            for device_id in persisted_ids - current_ids:
                await self.device_repository.delete(device_id)
                if not self.state_store.persistence_bound:
                    await self.state_snapshot_repository.delete(device_id)
                await self.state_store.delete(device_id)
            for device in self.registry.devices:
                await self.device_repository.save(device)
            if not self.state_store.persistence_bound:
                for state in await self.state_store.all():
                    await self.state_snapshot_repository.save(state)
            if self.runtime_state_metadata_repository is not None and not (
                self.state_store.persistence_bound
            ):
                await self.runtime_state_metadata_repository.save(
                    self.state_store.export_metadata()
                )
            elif self.state_store.persistence_bound:
                await self.state_store.persist_metadata()

        return DiscoveryResult(tuple(devices), tuple(areas), tuple(states), revision)

    async def refresh_state(self) -> tuple[StateSnapshot, ...]:
        """Refresh known state without rebuilding the device inventory."""

        async with self._refresh_lock:
            source_refs = [
                source_ref
                for device in self.registry.devices
                for source_ref in device.source_refs
            ]
            if not source_refs:
                return ()
            snapshots = await self.adapter.read_state(source_refs)
            return tuple(await self._save_state_snapshots_unlocked(snapshots))

    async def save_state_snapshots(
        self, snapshots: Sequence[StateSnapshot]
    ) -> tuple[StateSnapshot, ...]:
        """Persist event/read state through the same serialized boundary."""

        async with self._refresh_lock:
            return tuple(await self._save_state_snapshots_unlocked(snapshots))

    async def _save_state_snapshots_unlocked(
        self, snapshots: Sequence[StateSnapshot]
    ) -> list[StateSnapshot]:
        normalized_states: list[StateSnapshot] = []
        for snapshot in snapshots:
            canonical_id = self.registry.canonical_id_for_source(
                snapshot.source_ref.adapter_id, snapshot.source_ref.external_id
            )
            if canonical_id is None:
                continue
            normalized = snapshot.model_copy(update={"device_id": canonical_id})
            await self.state_store.save(normalized)
            normalized_states.append(normalized)
        return normalized_states

    def _inventory_fingerprint(self) -> str:
        routes = {
            (device.id, capability.name): self.registry.routes_for(device.id, capability.name)
            for device in self.registry.devices
            for capability in device.capabilities
        }
        return inventory_fingerprint(self.registry.devices, routes)

    def _configured_adapter_ids(self) -> frozenset[str]:
        """Return source IDs represented by this runtime's live adapter set."""

        children = getattr(self.adapter, "adapters", None)
        if children is None:
            return frozenset({self.adapter.adapter_id})
        adapter_ids = frozenset(str(child.adapter_id) for child in children)
        return adapter_ids or frozenset({self.adapter.adapter_id})

    async def _record_states(self, snapshot: AdapterSnapshot) -> list[StateSnapshot]:
        received_at = self.clock.now()
        states: list[StateSnapshot] = []
        seen_this_cycle: dict[tuple[str, str], StateSnapshot] = {}
        for raw_state in snapshot.source_states:
            external_id = str(raw_state["entity_id"])
            source_adapter_id = str(raw_state.get("source_adapter_id", self.adapter.adapter_id))
            device_id = self.registry.canonical_id_for_source(source_adapter_id, external_id)
            if device_id is None:
                continue
            status = (
                StateStatus.CURRENT if raw_state.get("available", True) else StateStatus.UNAVAILABLE
            )
            observed_at = _source_timestamp(raw_state.get("observed_at"), received_at)
            source_received_at = _source_timestamp(raw_state.get("received_at"), received_at)
            effective_received_at = max(source_received_at, observed_at)
            state = StateSnapshot(
                device_id=device_id,
                capability=str(raw_state["capability"]),
                value=raw_state.get("value"),
                unit=raw_state.get("unit"),
                observed_at=observed_at,
                received_at=effective_received_at,
                status=status,
                source_ref=SourceRef(
                    adapter_id=source_adapter_id,
                    external_id=external_id,
                ),
            )
            key = (device_id, state.capability)
            previous = seen_this_cycle.get(key)
            if (
                previous is not None
                and previous.source_ref != state.source_ref
                and (previous.value, previous.status) != (state.value, state.status)
            ):
                self.audit.append(
                    event_type="state_source_conflict",
                    actor="runtime",
                    subject_id=device_id,
                    payload={
                        "capability": state.capability,
                        "sources": [
                            {
                                "adapter_id": previous.source_ref.adapter_id,
                                "external_id": previous.source_ref.external_id,
                                "value": previous.value,
                                "status": previous.status.value,
                            },
                            {
                                "adapter_id": state.source_ref.adapter_id,
                                "external_id": state.source_ref.external_id,
                                "value": state.value,
                                "status": state.status.value,
                            },
                        ],
                        "retained_value": state.value,
                    },
                )
                state = state.model_copy(update={"status": StateStatus.INVALID, "value": None})
            seen_this_cycle[key] = state
            await self.state_store.save(state)
            states.append(state)
        return states


def _source_timestamp(value: object, fallback: datetime) -> datetime:
    """Preserve an adapter timestamp when it is valid and timezone-aware."""

    candidate = value
    if isinstance(candidate, str):
        try:
            candidate = datetime.fromisoformat(candidate)
        except ValueError:
            return fallback
    if not isinstance(candidate, datetime):
        return fallback
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        return fallback
    return candidate.astimezone(UTC)
