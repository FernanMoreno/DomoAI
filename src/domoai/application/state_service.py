"""State read use case with explicit stale-state behavior."""

from __future__ import annotations

from dataclasses import dataclass

from domoai.domain.models import StateSnapshot, StateStatus
from domoai.runtime.state_store import StateStore


@dataclass(frozen=True)
class StateReadDiagnostic:
    device_id: str
    capability: str
    reason: str
    status: StateStatus

    def as_dict(self) -> dict[str, str]:
        return {
            "device_id": self.device_id,
            "capability": self.capability,
            "reason": self.reason,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class StateReadResult:
    states: tuple[StateSnapshot, ...]
    diagnostics: tuple[StateReadDiagnostic, ...]


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
        result = await self.get_with_diagnostics(
            device_ids,
            capabilities,
            allow_stale=allow_stale,
        )
        return list(result.states)

    async def get_with_diagnostics(
        self,
        device_ids: list[str],
        capabilities: list[str] | None = None,
        *,
        allow_stale: bool = True,
    ) -> StateReadResult:
        wanted_capabilities = set(capabilities or [])
        states: list[StateSnapshot] = []
        diagnostics: list[StateReadDiagnostic] = []
        now = self.state_store.clock.now()
        for snapshot in await self.state_store.all():
            if snapshot.device_id not in device_ids:
                continue
            if wanted_capabilities and snapshot.capability not in wanted_capabilities:
                continue
            snapshot = self.state_store.effective_snapshot(snapshot, now)
            if not allow_stale and snapshot.status in {
                StateStatus.STALE,
                StateStatus.UNAVAILABLE,
                StateStatus.INVALID,
            }:
                diagnostics.append(
                    StateReadDiagnostic(
                        device_id=snapshot.device_id,
                        capability=snapshot.capability,
                        reason=snapshot.status.value,
                        status=snapshot.status,
                    )
                )
                continue
            states.append(snapshot)
        return StateReadResult(tuple(states), tuple(diagnostics))
