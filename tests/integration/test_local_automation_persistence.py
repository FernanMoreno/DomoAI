from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.automation import (
    AutomationConsent,
    AutomationRule,
    AutomationTrigger,
    automation_rule_digest,
)
from domoai.domain.models import Command, Plan
from domoai.persistence.repositories import AutomationRuleRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _record() -> tuple[AutomationRule, AutomationConsent]:
    rule = AutomationRule(
        id="rule-persisted",
        name="Persisted local rule",
        trigger=AutomationTrigger(
            type="state_changed", device_id="sensor.one", capability="motion", expected=True
        ),
        plan_template=Plan(
            id="rule-template",
            commands=[
                Command(
                    id="rule-command",
                    device_id="light.one",
                    command="turn_on",
                    idempotency_key="rule-intent",
                )
            ],
        ),
        scope="home",
        cooldown_seconds=60,
        status="enabled",
    )
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    consent = AutomationConsent(
        approval_id="approval-persisted",
        principal_id="operator",
        scope="home",
        rule_digest=automation_rule_digest(rule),
        approved_at=now,
        expires_at=now + timedelta(hours=1),
    )
    return rule, consent


@pytest.mark.asyncio
async def test_rule_and_consent_round_trip_and_atomic_event_claim(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "automation.sqlite3")
    await database.initialize()
    repository = AutomationRuleRepository(database)
    rule, consent = _record()

    await repository.save(rule, consent)
    restored = await repository.get(rule.id)

    assert restored is not None
    assert restored.rule == rule
    assert restored.consent == consent
    assert await repository.claim_event(rule.id, "event-1", datetime(2026, 9, 4, 12, tzinfo=UTC))
    assert not await repository.claim_event(
        rule.id, "event-1", datetime(2026, 9, 4, 12, tzinfo=UTC)
    )
    assert not await repository.claim_event(
        rule.id, "event-2", datetime(2026, 9, 4, 12, tzinfo=UTC)
    )


@pytest.mark.asyncio
async def test_rule_lifecycle_survives_restart(tmp_path) -> None:
    db_path = tmp_path / "automation-restart.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize()
    repository = AutomationRuleRepository(database)
    rule, consent = _record()
    await repository.save(rule, consent)
    assert await repository.set_status(rule.id, "disabled")
    await database.close()

    restarted = SQLiteDatabase(db_path)
    await restarted.initialize()
    restored = await AutomationRuleRepository(restarted).get(rule.id)

    assert restored is not None
    assert restored.rule.status.value == "disabled"


@pytest.mark.asyncio
async def test_rule_update_replaces_consent_and_invalidates_event_claim(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "automation-update.sqlite3")
    await database.initialize()
    repository = AutomationRuleRepository(database)
    rule, consent = _record()
    await repository.save(rule, consent)
    assert await repository.claim_event(rule.id, "event-before-update", consent.approved_at)

    updated_rule = AutomationRule.model_validate(
        {
            **rule.model_dump(mode="json"),
            "name": "Updated persisted local rule",
            "definition_digest": None,
        }
    )
    updated_consent = AutomationConsent(
        approval_id="approval-persisted-v2",
        principal_id="operator",
        scope=updated_rule.scope,
        rule_digest=updated_rule.definition_digest,
        approved_at=datetime(2026, 9, 4, 12, 1, tzinfo=UTC),
        expires_at=datetime(2026, 9, 4, 13, tzinfo=UTC),
    )

    assert await repository.update(
        updated_rule,
        updated_consent,
        expected_definition_digest=rule.definition_digest,
        expected_approval_id=consent.approval_id,
    )
    restored = await repository.get(rule.id)

    assert restored is not None
    assert restored.rule == updated_rule
    assert restored.consent == updated_consent
    assert restored.last_event_id is None
    assert restored.last_fired_at is None
