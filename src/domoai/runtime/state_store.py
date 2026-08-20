"""Freshness-aware in-memory state store."""

from __future__ import annotations

from datetime import datetime, timedelta

from domoai.domain.models import StateSnapshot, StateStatus
from domoai.runtime.clock import Clock, SystemClock


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

    def begin_revision(self) -> None:
        self._revision += 1

    @property
    def runtime_revision(self) -> str:
        return f"rev-{self._revision}"

    def state_version(self, device_id: str, capability: str) -> int:
        return self._state_versions.get((device_id, capability), 0)

    def load_persisted(self, snapshots: list[StateSnapshot]) -> None:
        """Restore last-known state from persistence, forced to stale."""

        for snapshot in snapshots:
            stale = snapshot.model_copy(update={"status": StateStatus.STALE})
            key = (stale.device_id, stale.capability)
            self._version_counter += 1
            self._state_versions[key] = self._version_counter
            self._snapshots[key] = stale

    async def save(self, snapshot: StateSnapshot) -> None:
        key = (snapshot.device_id, snapshot.capability)
        previous = self._snapshots.get(key)
        if previous is None or (previous.value, previous.status) != (
            snapshot.value,
            snapshot.status,
        ):
            self._version_counter += 1
            self._state_versions[key] = self._version_counter
        self._snapshots[key] = snapshot

    async def delete(self, device_id: str) -> None:
        for key in [key for key in self._snapshots if key[0] == device_id]:
            del self._snapshots[key]
            self._state_versions.pop(key, None)

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
