"""Freshness-aware in-memory state store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domoai.domain.models import StateSnapshot, StateStatus


class StateStore:
    def __init__(self, stale_after: timedelta = timedelta(minutes=5)) -> None:
        self.stale_after = stale_after
        self._snapshots: dict[tuple[str, str], StateSnapshot] = {}
        self._revision = 0

    def begin_revision(self) -> None:
        self._revision += 1

    @property
    def runtime_revision(self) -> str:
        return f"rev-{self._revision}"

    async def save(self, snapshot: StateSnapshot) -> None:
        self._snapshots[(snapshot.device_id, snapshot.capability)] = snapshot

    async def get(self, device_id: str, capability: str) -> StateSnapshot | None:
        return self._snapshots.get((device_id, capability))

    async def all(self) -> list[StateSnapshot]:
        return list(self._snapshots.values())

    async def mark_stale(self, now: datetime | None = None) -> list[StateSnapshot]:
        current_time = now or datetime.now(UTC)
        stale: list[StateSnapshot] = []
        for key, snapshot in list(self._snapshots.items()):
            if (
                snapshot.status is StateStatus.CURRENT
                and current_time - snapshot.received_at > self.stale_after
            ):
                updated = snapshot.model_copy(update={"status": StateStatus.STALE})
                self._snapshots[key] = updated
                stale.append(updated)
        return stale

    async def mark_all_stale(self) -> list[StateSnapshot]:
        """Mark every current cached value stale after source loss."""

        stale: list[StateSnapshot] = []
        for key, snapshot in list(self._snapshots.items()):
            if snapshot.status is StateStatus.CURRENT:
                updated = snapshot.model_copy(update={"status": StateStatus.STALE})
                self._snapshots[key] = updated
                stale.append(updated)
        return stale
