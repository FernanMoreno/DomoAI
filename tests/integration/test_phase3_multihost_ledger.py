from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.domain.coordination import PhysicalIntent, PhysicalIntentStatus
from domoai.persistence.coordination import PhysicalIntentRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _intent(key: str = "intent-1") -> PhysicalIntent:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    return PhysicalIntent(
        tenant_id="tenant-a",
        household_id="home-a",
        deployment_id="gw-a",
        idempotency_key=key,
        plan_id="plan-1",
        command_id="command-1",
        fencing_epoch=3,
        status=PhysicalIntentStatus.PREPARED,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_duplicate_claim_returns_one_durable_intent(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    try:
        repository = PhysicalIntentRepository(database)
        first = await repository.claim(_intent())
        duplicate = await repository.claim(_intent())

        assert first == duplicate
        assert first.status is PhysicalIntentStatus.PREPARED
        assert await repository.count() == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_confirmed_replay_and_inflight_recovery_are_idempotent(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    try:
        repository = PhysicalIntentRepository(database)
        await repository.claim(_intent("prepared"))
        await repository.claim(_intent("confirmed"))
        await repository.settle(
            household_id="home-a",
            idempotency_key="confirmed",
            status=PhysicalIntentStatus.CONFIRMED,
        )

        replay = await repository.claim(_intent("confirmed"))
        recovered = await repository.recover_inflight()
        recovered_again = await repository.recover_inflight()

        assert replay.status is PhysicalIntentStatus.CONFIRMED
        assert recovered == 1
        assert recovered_again == 0
        prepared = await repository.get(household_id="home-a", idempotency_key="prepared")
        assert prepared is not None
        assert prepared.status is PhysicalIntentStatus.UNKNOWN
    finally:
        await database.close()
