"""State read use case with explicit stale-state behavior."""

from __future__ import annotations

from domoai.domain.models import StateSnapshot
from domoai.runtime.state_store import StateStore


class StateService:
    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store

    async def get(
        self,
        device_ids: list[str],
        capabilities: list[str] | None = None,
        *,
        allow_stale: bool = True,
    ) -> list[StateSnapshot]:
        wanted_capabilities = set(capabilities or [])
        result: list[StateSnapshot] = []
        for snapshot in await self.state_store.all():
            if snapshot.device_id not in device_ids:
                continue
            if wanted_capabilities and snapshot.capability not in wanted_capabilities:
                continue
            if not allow_stale and snapshot.status.value in {"stale", "unavailable"}:
                continue
            result.append(snapshot)
        return result
