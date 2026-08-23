from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.domain.models import (
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    BundleMemberCommitStatus,
    Command,
    ExecutionStatus,
    Plan,
    PlanStatus,
)
from domoai.persistence.repositories import BundleCommitRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _future_plan(plan_id: str, hours: int) -> Plan:
    return Plan(
        id=plan_id,
        status=PlanStatus.READY,
        execute_at=datetime.now(UTC) + timedelta(hours=hours),
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id="light.one",
                command="turn_on",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_future_fulfillment_recomputes_aggregate_and_survives_restart(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "bundle-fulfillment.sqlite3")
    await database.initialize()
    repository = BundleCommitRepository(database)
    first = _future_plan("fulfillment-first", 1)
    second = _future_plan("fulfillment-second", 2)
    bundle = BundleCommit(
        id="fulfillment-bundle",
        bundle_digest="sha256:fulfillment-bundle",
        scenario_id="fulfillment-scenario",
        members=[
            BundleMemberCommit(
                plan_id=first.id,
                validation_digest="sha256:first",
                execute_at=first.execute_at,
            ),
            BundleMemberCommit(
                plan_id=second.id,
                validation_digest="sha256:second",
                execute_at=second.execute_at,
            ),
        ],
    )
    await repository.save(bundle)
    scheduled = await repository.schedule_members_transaction(
        bundle, [first, second], [0, 1], final_status=BundleCommitStatus.SCHEDULED
    )
    assert scheduled.status is BundleCommitStatus.SCHEDULED
    assert all(member.status is BundleMemberCommitStatus.SCHEDULED for member in scheduled.members)

    after_first = await repository.record_member_outcome(
        first.id,
        status=BundleMemberCommitStatus.EXECUTED,
        execution_status=ExecutionStatus.CONFIRMED_SUCCESS,
        details={"execution": "confirmed"},
    )
    assert after_first is not None
    assert after_first.status is BundleCommitStatus.SCHEDULED
    assert after_first.members[0].status is BundleMemberCommitStatus.EXECUTED

    after_second = await repository.record_member_outcome(
        second.id,
        status=BundleMemberCommitStatus.FAILED,
        execution_status=ExecutionStatus.FAILED,
        details={"execution": "failed"},
    )
    assert after_second is not None
    assert after_second.status is BundleCommitStatus.PARTIALLY_COMMITTED
    assert after_second.members[1].status is BundleMemberCommitStatus.FAILED

    duplicate = await repository.record_member_outcome(
        second.id,
        status=BundleMemberCommitStatus.FAILED,
        execution_status=ExecutionStatus.FAILED,
        details={"execution": "failed-again"},
    )
    assert duplicate is not None
    assert duplicate.members[1].details == {"execution": "failed"}

    restarted = BundleCommitRepository(database)
    restored = await restarted.get(bundle.id)
    assert restored is not None
    assert restored.status is BundleCommitStatus.PARTIALLY_COMMITTED
    assert [member.status for member in restored.members] == [
        BundleMemberCommitStatus.EXECUTED,
        BundleMemberCommitStatus.FAILED,
    ]


@pytest.mark.composition
@pytest.mark.asyncio
async def test_all_missed_members_have_explicit_missed_aggregate(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "bundle-missed.sqlite3")
    await database.initialize()
    repository = BundleCommitRepository(database)
    plan = _future_plan("missed-member", 1)
    bundle = BundleCommit(
        id="missed-bundle",
        bundle_digest="sha256:missed-bundle",
        scenario_id="missed-scenario",
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest="sha256:missed",
                execute_at=plan.execute_at,
            )
        ],
    )
    await repository.save(bundle)
    scheduled = await repository.schedule_members_transaction(
        bundle, [plan], [0], final_status=BundleCommitStatus.SCHEDULED
    )

    settled = await repository.record_member_outcome(
        plan.id,
        status=BundleMemberCommitStatus.MISSED,
        execution_status=None,
        details={"reason": "grace_window_expired"},
    )

    assert scheduled.status is BundleCommitStatus.SCHEDULED
    assert settled is not None
    assert settled.status is BundleCommitStatus.MISSED
    assert settled.members[0].status is BundleMemberCommitStatus.MISSED
