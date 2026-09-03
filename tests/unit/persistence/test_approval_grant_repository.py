from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalGrant


def _grant() -> ApprovalGrant:
    now = datetime.now(UTC)
    return ApprovalGrant(
        approval_id="approval-1",
        plan_id="plan-1",
        validation_digest="sha256:validation",
        approved_by="operator-1",
        issued_at=now,
        authentication_context="operator-ui",
        session_id="session-1",
        bundle_digest="sha256:bundle",
        recurrence_digest=None,
        validation_valid_until=now + timedelta(minutes=10),
        window_digest="sha256:window",
        schedule_revision=4,
        assertion_nonce="nonce-1",
        approved_at=now,
        expires_at=now + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_approval_grant_survives_restart_with_scope_intact(tmp_path: Path) -> None:
    from domoai.persistence.repositories import ApprovalGrantRepository

    path = tmp_path / "authority.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    repository = ApprovalGrantRepository(database)
    grant = _grant()
    await repository.save(grant)
    await database.close()

    restarted_database = SQLiteDatabase(path)
    await restarted_database.initialize()
    restored = await ApprovalGrantRepository(restarted_database).get(grant.approval_id)

    assert restored == grant
    await restarted_database.close()


@pytest.mark.asyncio
async def test_approval_grant_consumption_is_atomic_and_one_shot(tmp_path: Path) -> None:
    from domoai.persistence.repositories import ApprovalGrantRepository

    database = SQLiteDatabase(tmp_path / "authority.sqlite3")
    await database.initialize()
    repository = ApprovalGrantRepository(database)
    grant = _grant()
    await repository.save(grant)
    now = datetime.now(UTC)

    assert await repository.consume_if_pending(grant.approval_id, now=now) is True
    assert await repository.consume_if_pending(grant.approval_id, now=now) is False
    assert repository.is_consumed_sync(grant.approval_id) is True


@pytest.mark.asyncio
async def test_consumed_approval_status_survives_repository_restart(tmp_path: Path) -> None:
    from domoai.persistence.repositories import ApprovalGrantRepository

    path = tmp_path / "authority-restart.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    grant = _grant()
    repository = ApprovalGrantRepository(database)
    await repository.save(grant)
    assert await repository.consume_if_pending(grant.approval_id, now=datetime.now(UTC)) is True
    await database.close()

    restarted = SQLiteDatabase(path)
    await restarted.initialize()
    restored_repository = ApprovalGrantRepository(restarted)

    assert restored_repository.is_consumed_sync(grant.approval_id) is True
    assert await restored_repository.get(grant.approval_id) == grant
    await restarted.close()

    await database.close()
