from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.domain.automation import (
    AutomationCondition,
    AutomationConsent,
    AutomationEvent,
    AutomationRule,
    AutomationTrigger,
    automation_rule_digest,
)
from domoai.domain.models import Command, Plan


def _plan() -> Plan:
    return Plan(
        id="automation-template",
        commands=[
            Command(
                id="automation-command",
                device_id="living_room.main_light",
                command="turn_on",
                idempotency_key="automation-intent",
            )
        ],
    )


def _rule() -> AutomationRule:
    return AutomationRule(
        id="rule-evening-light",
        name="Evening light",
        trigger=AutomationTrigger(
            type="state_changed",
            device_id="hall.sensor",
            capability="occupancy",
            expected=True,
        ),
        conditions=[
            AutomationCondition(
                device_id="hall.sensor", capability="occupancy", expected=True
            )
        ],
        plan_template=_plan(),
        scope="home:living-room",
        cooldown_seconds=30,
    )


def test_rule_digest_is_stable_and_excludes_lifecycle_status() -> None:
    rule = _rule()
    changed_lifecycle = rule.model_copy(update={"status": "disabled"})

    assert automation_rule_digest(rule) == automation_rule_digest(changed_lifecycle)
    assert rule.definition_digest == automation_rule_digest(rule)


def test_state_trigger_requires_typed_match_fields() -> None:
    with pytest.raises(ValidationError):
        AutomationTrigger(type="state_changed")


def test_time_trigger_requires_timezone_and_time() -> None:
    with pytest.raises(ValidationError):
        AutomationTrigger(type="time", timezone="Europe/Madrid")
    with pytest.raises(ValidationError):
        AutomationTrigger(type="time", time_of_day="22:00")


def test_consent_must_bind_rule_scope_and_have_future_expiry() -> None:
    rule = _rule()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    consent = AutomationConsent(
        approval_id="approval-rule",
        principal_id="operator-1",
        scope=rule.scope,
        rule_digest=rule.definition_digest,
        approved_at=now,
        expires_at=now + timedelta(hours=1),
    )

    assert consent.scope == rule.scope
    with pytest.raises(ValidationError):
        AutomationConsent(
            approval_id="approval-rule",
            principal_id="operator-1",
            scope=rule.scope,
            rule_digest=rule.definition_digest,
            approved_at=now,
            expires_at=now,
        )


def test_event_identity_is_explicit_and_values_are_scalar() -> None:
    event = AutomationEvent(
        event_id="event-1",
        event_type="state_changed",
        occurred_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
        device_id="hall.sensor",
        capability="occupancy",
        value=True,
    )

    assert event.event_id == "event-1"
    with pytest.raises(ValidationError):
        AutomationEvent(
            event_id="event-2",
            event_type="state_changed",
            occurred_at=event.occurred_at,
            device_id="hall.sensor",
            capability="occupancy",
            value={"raw": "vendor-payload"},
        )
