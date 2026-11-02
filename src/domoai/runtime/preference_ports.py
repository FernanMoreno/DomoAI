"""Domain-only storage boundary; application owns authority and inventory."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from domoai.domain.household_preferences import (
    HouseholdPreference,
    Operation,
    PreferenceHistoryEntry,
    PreferenceMutation,
    PreferenceResult,
    PreferenceSlotMutation,
    PreferenceVersion,
)


class PreferenceRepositoryPort(Protocol):
    tenant_id: str
    household_id: str

    async def snapshot(self) -> tuple[PreferenceVersion, list[HouseholdPreference]]: ...

    async def mutate(
        self,
        *,
        operation: Operation,
        request: PreferenceMutation | PreferenceSlotMutation,
        principal_id: str,
        now: datetime,
        fingerprint: str | None = None,
    ) -> PreferenceResult: ...

    async def replay(
        self,
        *,
        request: PreferenceMutation | PreferenceSlotMutation,
        principal_id: str,
        fingerprint: str,
    ) -> PreferenceResult | None: ...

    async def history(
        self, *, after_revision: int = 0, limit: int = 100, preference_id: str | None = None
    ) -> tuple[PreferenceVersion, list[PreferenceHistoryEntry]]: ...

    async def history_source(
        self, preference_id: UUID, revision: int
    ) -> PreferenceHistoryEntry | None: ...
