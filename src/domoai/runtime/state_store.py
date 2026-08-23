"""Freshness-aware in-memory state store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from domoai.domain.models import StateSnapshot, StateStatus
from domoai.runtime.clock import Clock, SystemClock


@dataclass(frozen=True)
class StateStoreMetadata:
    """Durable revision/version counters used by validated plan dependencies."""

    inventory_revision: int
    version_counter: int
    state_versions: dict[tuple[str, str], int]
    inventory_fingerprint: str | None = None


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
        self._revision = 0
        self._state_versions: dict[tuple[str, str], int] = {}
        self._version_counter = 0
        self._inventory_fingerprint: str | None = None
        self._startup_reconfirmation: dict[tuple[str, str], tuple[object, StateStatus]] = {}

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
            key = (stale.device_id, stale.capability)
            if key not in self._state_versions:
                self._version_counter += 1
                self._state_versions[key] = self._version_counter
            self._startup_reconfirmation[key] = (snapshot.value, snapshot.status)
            self._snapshots[key] = stale

    async def save(self, snapshot: StateSnapshot) -> None:
        key = (snapshot.device_id, snapshot.capability)
        previous = self._snapshots.get(key)
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

    async def delete(self, device_id: str) -> None:
        for key in [key for key in self._snapshots if key[0] == device_id]:
            del self._snapshots[key]
            self._state_versions.pop(key, None)
            self._startup_reconfirmation.pop(key, None)

    def peek(self, device_id: str, capability: str) -> StateSnapshot | None:
        """Return the cached snapshot without performing I/O or refreshing it."""

        return self._snapshots.get((device_id, capability))

    async def get(self, device_id: str, capability: str) -> StateSnapshot | None:
        return self._snapshots.get((device_id, capability))

    async def all(self) -> list[StateSnapshot]:
        return list(self._snapshots.values())

    async def mark_stale(self, now: datetime | None = None) -> list[StateSnapshot]:
        current_time = now or self.clock.now()
        stale: list[StateSnapshot] = []
        for key, snapshot in list(self._snapshots.items()):
            if (
                snapshot.status is StateStatus.CURRENT
                and current_time - snapshot.received_at > self.stale_after
            ):
                updated = snapshot.model_copy(update={"status": StateStatus.STALE})
                self._snapshots[key] = updated
                self._version_counter += 1
                self._state_versions[key] = self._version_counter
                stale.append(updated)
        return stale

    async def mark_all_stale(self) -> list[StateSnapshot]:
        """Mark every current cached value stale after source loss."""

        stale: list[StateSnapshot] = []
        for key, snapshot in list(self._snapshots.items()):
            if snapshot.status is StateStatus.CURRENT:
                updated = snapshot.model_copy(update={"status": StateStatus.STALE})
                self._snapshots[key] = updated
                self._version_counter += 1
                self._state_versions[key] = self._version_counter
                stale.append(updated)
        return stale
