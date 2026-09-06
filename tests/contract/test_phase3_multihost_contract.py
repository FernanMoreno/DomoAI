from datetime import UTC, datetime

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
        status=PhysicalIntentStatus.PREPARED,
        created_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
        updated_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
    )

    payload = intent.model_dump_json()
    assert "bearer" not in payload
    assert "token" not in payload
    assert "provider_payload" not in payload
