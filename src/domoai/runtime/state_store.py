"""Freshness-aware in-memory state store."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from domoai.domain.models import StateSnapshot, StateStatus
from domoai.runtime.clock import Clock, SystemClock


@dataclass(frozen=True)
class StateStoreMetadata:
    """Durable revision/version counters used by validated plan dependencies."""

    inventory_revision: int
    version_counter: int
    state_versions: dict[tuple[str, str], int]
    inventory_fingerprint: str | None = None


class RuntimeStatePersistencePort(Protocol):
    async def persist(
        self, snapshots: Sequence[StateSnapshot], metadata: StateStoreMetadata
    ) -> None: ...

    async def delete(self, device_id: str, metadata: StateStoreMetadata) -> None: ...

    async def delete_capability(
        self, device_id: str, capability: str, metadata: StateStoreMetadata
    ) -> None: ...


class StateStore:
    def __init__(
        self,
        stale_after: timedelta = timedelta(minutes=5),
        *,
        clock: Clock | None = None,
    ) -> None:
        self.stale_after = stale_after
        self.clock = clock or SystemClock()
        self._snapshots: dict[tuple[str, str], StateSnapshot] = {}
        self._source_snapshots: dict[tuple[str, str, str, str], StateSnapshot] = {}
        self._revision = 0
        self._state_versions: dict[tuple[str, str], int] = {}
        self._version_counter = 0
        self._inventory_fingerprint: str | None = None
        self._startup_reconfirmation: dict[tuple[str, str], tuple[object, StateStatus]] = {}
        self._persistence: RuntimeStatePersistencePort | None = None

    def bind_persistence(self, persistence: RuntimeStatePersistencePort) -> None:
        """Attach the one durable writer used by every mutation path."""

        self._persistence = persistence

    @property
    def persistence_bound(self) -> bool:
        return self._persistence is not None

    def begin_revision(self) -> None:
        self._revision += 1

    @property
    def runtime_revision(self) -> str:
        return f"rev-{self._revision}"

    def state_version(self, device_id: str, capability: str) -> int:
        return self._state_versions.get((device_id, capability), 0)

    def restore_metadata(self, metadata: StateStoreMetadata) -> None:
        """Restore durable counters before persisted snapshots are loaded."""

        self._revision = max(0, metadata.inventory_revision)
        self._version_counter = max(
            metadata.version_counter,
            max(metadata.state_versions.values(), default=0),
        )
        self._state_versions = {
            key: version for key, version in metadata.state_versions.items() if version >= 0
        }
        self._inventory_fingerprint = metadata.inventory_fingerprint

    @property
    def inventory_fingerprint(self) -> str | None:
        return self._inventory_fingerprint

    def record_inventory_fingerprint(self, fingerprint: str) -> None:
        self._inventory_fingerprint = fingerprint

    def export_metadata(self) -> StateStoreMetadata:
        return StateStoreMetadata(
            inventory_revision=self._revision,
            version_counter=self._version_counter,
            state_versions=dict(self._state_versions),
            inventory_fingerprint=self._inventory_fingerprint,
        )

    def load_persisted(self, snapshots: list[StateSnapshot]) -> None:
        """Restore last-known state from persistence, forced to stale."""

        for snapshot in snapshots:
            stale = snapshot.model_copy(update={"status": StateStatus.STALE})
            source_key = (
                stale.device_id,
                stale.capability,
                stale.source_ref.adapter_id,
                stale.source_ref.external_id,
            )
            self._source_snapshots[source_key] = stale
            key = (stale.device_id, stale.capability)
            if key not in self._state_versions:
                self._version_counter += 1
                self._state_versions[key] = self._version_counter
            self._startup_reconfirmation[key] = (snapshot.value, snapshot.status)
            self._snapshots[key] = stale

    async def save(self, snapshot: StateSnapshot) -> None:
        key = (snapshot.device_id, snapshot.capability)
        source_key = (
            snapshot.device_id,
            snapshot.capability,
            snapshot.source_ref.adapter_id,
            snapshot.source_ref.external_id,
        )
        previous = self._snapshots.get(key)
        self._source_snapshots[source_key] = snapshot
        snapshot = self._resolve_sources(snapshot.device_id, snapshot.capability)
        startup_value = self._startup_reconfirmation.pop(key, None)
        if startup_value is not None:
            changed = (snapshot.value, snapshot.status) != startup_value
        else:
            changed = previous is None or (previous.value, previous.status) != (
                snapshot.value,
                snapshot.status,
            )
        if changed:
            self._version_counter += 1
            self._state_versions[key] = self._version_counter
        self._snapshots[key] = snapshot
        await self._persist([snapshot])

    def _resolve_sources(self, device_id: str, capability: str) -> StateSnapshot:
        observations = [
            observation
            for (
                source_device,
                source_capability,
                _adapter,
                _external,
            ), observation in self._source_snapshots.items()
            if source_device == device_id and source_capability == capability
        ]
        if not observations:
            raise KeyError(f"missing source observation for {device_id}/{capability}")
        invalid = [item for item in observations if item.status is StateStatus.INVALID]
        if invalid:
            return max(invalid, key=lambda item: item.received_at)
        current = [item for item in observations if item.status is StateStatus.CURRENT]
        if len(current) >= 2 and len({repr(item.value) for item in current}) > 1:
            latest = max(current, key=lambda item: item.received_at)
            return latest.model_copy(update={"value": None, "status": StateStatus.INVALID})
        candidates = current or observations
        return max(candidates, key=lambda item: item.received_at)

    async def delete(self, device_id: str) -> None:
        for key in [key for key in self._snapshots if key[0] == device_id]:
            del self._snapshots[key]
            self._state_versions.pop(key, None)
            self._startup_reconfirmation.pop(key, None)
        if self._persistence is not None:
            await self._persistence.delete(device_id, self.export_metadata())

    async def delete_capability(self, device_id: str, capability: str) -> bool:
        """Remove state no longer advertised by the authoritative inventory."""

        key = (device_id, capability)
        existed = key in self._snapshots or any(
            source_key[:2] == key for source_key in self._source_snapshots
        )
        if not existed:
            return False
        self._snapshots.pop(key, None)
        self._state_versions.pop(key, None)
        self._startup_reconfirmation.pop(key, None)
        for source_key in [
            source_key
            for source_key in self._source_snapshots
            if source_key[:2] == (device_id, capability)
        ]:
            del self._source_snapshots[source_key]
        if self._persistence is not None:
            delete_capability = getattr(self._persistence, "delete_capability", None)
            if callable(delete_capability):
                await delete_capability(device_id, capability, self.export_metadata())
        return True

    async def prune_capabilities(self, device_id: str, capabilities: set[str]) -> list[str]:
        """Delete cached capabilities removed from a live source mapping."""

        removed = [
            capability
            for current_device, capability in self._snapshots
            if current_device == device_id and capability not in capabilities
        ]
        for capability in removed:
            await self.delete_capability(device_id, capability)
        return removed

    def peek(self, device_id: str, capability: str) -> StateSnapshot | None:
        """Return the cached snapshot without performing I/O or refreshing it."""

        return self._snapshots.get((device_id, capability))

    async def get(self, device_id: str, capability: str) -> StateSnapshot | None:
        return self._snapshots.get((device_id, capability))

    async def all(self) -> list[StateSnapshot]:
        return list(self._snapshots.values())

    def effective_status(
        self, snapshot: StateSnapshot, now: datetime | None = None
    ) -> StateStatus:
        """Return the server-owned status at the instant of the query.

        ``observed_at`` remains source provenance.  ``received_at`` is the
        latest runtime confirmation of that value and is therefore the clock
        used for JIT freshness.  This lets an active Home Assistant read
        confirm an unchanged value without rewriting its source observation
        time, while cached adapters remain stale when their last receipt ages.
        """

        if snapshot.status is not StateStatus.CURRENT:
            return snapshot.status
        current_time = now or self.clock.now()
        if snapshot.observed_at > current_time or snapshot.received_at > current_time:
            return StateStatus.INVALID
        if current_time - snapshot.received_at > self.stale_after:
            return StateStatus.STALE
        return StateStatus.CURRENT

    def effective_snapshot(
        self, snapshot: StateSnapshot, now: datetime | None = None
    ) -> StateSnapshot:
        """Project JIT freshness without mutating the persisted snapshot."""

        status = self.effective_status(snapshot, now)
        if status is StateStatus.INVALID and snapshot.status is StateStatus.CURRENT:
            return snapshot.model_copy(update={"status": status, "value": None})
        if status is snapshot.status:
            return snapshot
        return snapshot.model_copy(update={"status": status})

    def freshness_report(
        self,
        *,
        optional_sources: frozenset[tuple[str, str]] = frozenset(),
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Return the canonical freshness view used by health and readiness."""

        current_time = now or self.clock.now()
        projected = [
            self.effective_snapshot(item, current_time) for item in self._snapshots.values()
        ]
        required = [
            item
            for item in projected
            if (item.source_ref.adapter_id, item.source_ref.external_id) not in optional_sources
        ]
        optional = [
            item
            for item in projected
            if (item.source_ref.adapter_id, item.source_ref.external_id) in optional_sources
        ]
        statuses = {item.status for item in required}
        if not required:
            status = "unknown"
        elif StateStatus.INVALID in statuses or StateStatus.UNAVAILABLE in statuses:
            status = "degraded"
        elif StateStatus.STALE in statuses:
            status = "stale"
        else:
            status = "current"
        reason_codes: list[str] = []
        if StateStatus.INVALID in statuses:
            reason_codes.append("state_invalid")
        if StateStatus.UNAVAILABLE in statuses:
            reason_codes.append("state_unavailable")
        if StateStatus.STALE in statuses:
            reason_codes.append("state_stale")
        optional_statuses = {item.status for item in optional}
        if StateStatus.INVALID in optional_statuses:
            reason_codes.append("optional_state_invalid")
        if StateStatus.UNAVAILABLE in optional_statuses:
            reason_codes.append("optional_state_unavailable")
        if StateStatus.STALE in optional_statuses:
            reason_codes.append("optional_state_stale")
        ages = [
            max(0.0, (current_time - item.received_at).total_seconds())
            for item in required
        ]
        return {
            "status": status,
            "max_age_seconds": max(ages, default=None),
            "stale_after_seconds": self.stale_after.total_seconds(),
            "required_snapshot_count": len(required),
            "optional_snapshot_count": len(optional),
            "reason_codes": reason_codes,
        }

    def max_state_age_seconds(self, now: datetime | None = None) -> float | None:
        """Return the oldest cached observation age for health reporting."""

        if not self._snapshots:
            return None
        current_time = now or self.clock.now()
        return max(
            0.0,
            max(
                (current_time - snapshot.received_at).total_seconds()
                for snapshot in self._snapshots.values()
            ),
        )

    async def mark_stale(self, now: datetime | None = None) -> list[StateSnapshot]:
        current_time = now or self.clock.now()
        stale: list[StateSnapshot] = []
        for key, snapshot in list(self._snapshots.items()):
            if (
                snapshot.status is StateStatus.CURRENT
                and self.effective_status(snapshot, current_time) is StateStatus.STALE
            ):
                updated = snapshot.model_copy(update={"status": StateStatus.STALE})
                self._snapshots[key] = updated
                self._version_counter += 1
                self._state_versions[key] = self._version_counter
                for source_key, source_snapshot in list(self._source_snapshots.items()):
                    if source_key[:2] == key and source_snapshot.source_ref == snapshot.source_ref:
                        self._source_snapshots[source_key] = updated
                stale.append(updated)
        await self._persist(stale)
        return stale

    async def mark_source_unavailable(self, adapter_id: str) -> list[StateSnapshot]:
        """Degrade only observations owned by one disconnected source."""

        changed: list[StateSnapshot] = []
        affected: set[tuple[str, str]] = set()
        for source_key, snapshot in list(self._source_snapshots.items()):
            if source_key[2] != adapter_id or snapshot.status is StateStatus.UNAVAILABLE:
                continue
            self._source_snapshots[source_key] = snapshot.model_copy(
                update={"status": StateStatus.UNAVAILABLE, "value": None}
            )
            affected.add(source_key[:2])
        for key in affected:
            resolved = self._resolve_sources(*key)
            self._snapshots[key] = resolved
            self._version_counter += 1
            self._state_versions[key] = self._version_counter
            changed.append(resolved)
        await self._persist(changed)
        return changed

    async def mark_all_stale(self) -> list[StateSnapshot]:
        """Mark every current cached value stale after source loss."""

        stale: list[StateSnapshot] = []
        for key, snapshot in list(self._snapshots.items()):
            if snapshot.status is StateStatus.CURRENT:
                updated = snapshot.model_copy(update={"status": StateStatus.STALE})
                self._snapshots[key] = updated
                for source_key, source_snapshot in list(self._source_snapshots.items()):
                    if source_key[:2] == key and source_snapshot.status is StateStatus.CURRENT:
                        self._source_snapshots[source_key] = source_snapshot.model_copy(
                            update={"status": StateStatus.STALE}
                        )
                self._version_counter += 1
                self._state_versions[key] = self._version_counter
                stale.append(updated)
        await self._persist(stale)
        return stale

    async def persist_metadata(self) -> None:
        """Flush revision-only changes such as inventory fingerprints."""

        if self._persistence is not None:
            await self._persistence.persist((), self.export_metadata())

    async def _persist(self, snapshots: Sequence[StateSnapshot]) -> None:
        if self._persistence is not None:
            await self._persistence.persist(snapshots, self.export_metadata())
