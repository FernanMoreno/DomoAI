from datetime import UTC, datetime

import pytest

from domoai.domain.coordination import PhysicalIntent, PhysicalIntentStatus


def test_physical_intent_contains_no_bearer_or_provider_payload() -> None:
    intent = PhysicalIntent(
        tenant_id="tenant-a",
        household_id="home-a",
        deployment_id="gw-a",
        idempotency_key="safe-intent",
        plan_id="plan-a",
        command_id="command-a",
        fencing_epoch=1,
        status=PhysicalIntentStatus.DISPATCHING,
        owner_id="owner-a",
        execution_attempt_id="attempt-a",
        created_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
        updated_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
    )

    payload = intent.model_dump_json()
    assert "bearer" not in payload
    assert "token" not in payload
    assert "provider_payload" not in payload


@pytest.mark.parametrize("status", list(PhysicalIntentStatus))
def test_intent_wire_states_preserve_identity_and_remain_backward_compatible(status) -> None:
    payload = {
        "tenant_id": "tenant-a",
        "household_id": "home-a",
        "deployment_id": "gw-a",
        "idempotency_key": "intent-a",
        "plan_id": "plan-a",
        "command_id": "command-a",
        "fencing_epoch": 3,
        "status": status.value,
        "owner_id": "owner-a",
        "execution_attempt_id": "attempt-a",
        "created_at": "2026-09-10T12:00:00Z",
        "updated_at": "2026-09-10T12:00:00Z",
    }
    # Older persisted payloads do not carry a lease; no migration is required.
    intent = PhysicalIntent.model_validate(payload)
    assert intent.lease_id is None
    assert PhysicalIntent.model_validate_json(intent.model_dump_json()) == intent
    assert intent.model_dump(mode="json")["status"] == status.value
