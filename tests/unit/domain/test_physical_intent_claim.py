from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domoai.domain.coordination import (
    PhysicalIntent,
    PhysicalIntentClaim,
    PhysicalIntentClaimDecision,
    PhysicalIntentStatus,
)


def _intent(**updates: object) -> PhysicalIntent:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "household_id": "home-a",
        "deployment_id": "gw-a",
        "idempotency_key": "intent-1",
        "plan_id": "plan-1",
        "command_id": "command-1",
        "fencing_epoch": 3,
        "status": PhysicalIntentStatus.DISPATCHING,
        "owner_id": "owner-a",
        "execution_attempt_id": "attempt-a",
        "created_at": now,
        "updated_at": now,
    }
    values.update(updates)
    return PhysicalIntent.model_validate(values)


def test_dispatching_intent_requires_owner_and_attempt() -> None:
    with pytest.raises(ValidationError):
        _intent(owner_id=None)
    with pytest.raises(ValidationError):
        _intent(execution_attempt_id=None)


def test_claim_decision_is_explicit_and_claim_is_immutable() -> None:
    claim = PhysicalIntentClaim(
        decision=PhysicalIntentClaimDecision.ACQUIRED,
        intent=_intent(),
    )

    assert claim.decision is PhysicalIntentClaimDecision.ACQUIRED
    with pytest.raises(FrozenInstanceError):
        claim.decision = PhysicalIntentClaimDecision.REPLAY  # type: ignore[misc]
