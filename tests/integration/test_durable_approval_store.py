from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.domain.errors import DomainError
from domoai.domain.models import (
    Command,
    Plan,
    PlanStatus,
    PolicyAction,
    PolicyDecision,
    ValidationResult,
    ValidationStatus,
)
from domoai.persistence.repositories import ApprovalGrantRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore


def _plan() -> Plan:
    return Plan(
        id="durable-approval-plan",
        commands=[
            Command(
                id="durable-approval-command",
                device_id="cover.garage_main",
                command="open",
                idempotency_key="durable-approval-key",
            )
        ],
        status=PlanStatus.REQUIRES_CONFIRMATION,
        validation=ValidationResult(
            status=ValidationStatus.REQUIRES_CONFIRMATION,
            validated_at=datetime.now(UTC),
            runtime_revision="revision-1",
            digest="sha256:durable-approval",
        ),
        policy_decisions=[PolicyDecision(action=PolicyAction.CONFIRM, reason="test")],
    )


@pytest.mark.asyncio
async def test_approval_store_restores_pending_grants_and_keeps_consumption_one_shot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval-store.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    store = ApprovalStore(
        operator_token="operator",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(database),
    )
    plan = _plan()
    grant = store.issue(plan, approved_by="operator", operator_token="operator")
    await database.close()

    restarted_database = SQLiteDatabase(path)
    await restarted_database.initialize()
    restarted_store = ApprovalStore(
        operator_token="operator",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(restarted_database),
    )

    assert restarted_store.consume(grant.approval_id, plan).approval_id == grant.approval_id
    with pytest.raises(DomainError):
        restarted_store.consume(grant.approval_id, plan)

    await restarted_database.close()
