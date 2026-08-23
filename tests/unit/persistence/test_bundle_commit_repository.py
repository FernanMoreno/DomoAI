from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.domain.models import (
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    Command,
    Plan,
    PlanStatus,
)
from domoai.persistence.repositories import BundleCommitRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _plan(plan_id: str, offset_hours: int) -> Plan:
    return Plan(
        id=plan_id,
        execute_at=datetime.now(UTC) + timedelta(hours=offset_hours),
        status=PlanStatus.READY,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id="light.one",
                command="turn_on",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )


@pytest.mark.asyncio
async def test_bundle_repository_preserves_order_and_rolls_back_schedule_batch(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "bundle-repository.sqlite3")
    await database.initialize()
    repository = BundleCommitRepository(database)
    first = _plan("future-1", 1)
    second = _plan("future-2", 2)
    bundle = BundleCommit(
        id="bundle-repository-1",
        bundle_digest="sha256:bundle-repository-1",
        scenario_id="scenario-1",
        members=[
            BundleMemberCommit(plan_id=first.id, validation_digest="sha256:first"),
            BundleMemberCommit(plan_id=second.id, validation_digest="sha256:second"),
        ],
    )
    await repository.save(bundle)

    persisted = await repository.get_by_digest(bundle.bundle_digest)
    assert persisted is not None
    assert [member.plan_id for member in persisted.members] == [first.id, second.id]

    database.connection.execute(
        """
        CREATE TRIGGER fail_second_bundle_schedule
        BEFORE INSERT ON scheduled_plans
        WHEN NEW.plan_id = 'future-2'
        BEGIN
            SELECT RAISE(ABORT, 'injected schedule failure');
        END
        """
    )
    database.connection.commit()

    with pytest.raises(Exception, match="injected schedule failure"):
        await repository.schedule_members_transaction(
            bundle,
            [first, second],
            [0, 1],
            final_status=BundleCommitStatus.SCHEDULED,
        )

    pending_count = database.connection.execute(
        "SELECT COUNT(*) FROM scheduled_plans WHERE status = 'pending'"
    ).fetchone()[0]
    assert pending_count == 0
    unchanged = await repository.get(bundle.id)
    assert unchanged is not None
    assert unchanged.status is BundleCommitStatus.COMMITTING
    assert [member.status.value for member in unchanged.members] == ["pending", "pending"]


@pytest.mark.asyncio
async def test_scheduled_bundle_member_is_protected_from_generic_reschedule(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "bundle-member.sqlite3")
    await database.initialize()
    repository = BundleCommitRepository(database)
    bundle = BundleCommit(
        id="bundle-member-1",
        bundle_digest="sha256:bundle-member-1",
        scenario_id="scenario-member-1",
        status=BundleCommitStatus.SCHEDULED,
        members=[
            BundleMemberCommit(
                plan_id="scheduled-member-1",
                validation_digest="sha256:member",
                status="scheduled",
                scheduled=True,
            )
        ],
    )
    await repository.save(bundle)

    assert await repository.is_scheduled_member("scheduled-member-1") is True
    assert await repository.is_scheduled_member("other-plan") is False
