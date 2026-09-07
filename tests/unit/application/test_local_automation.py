from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from domoai.application.local_automation import LocalAutomationEngine
from domoai.domain.automation import (
    AutomationConsent,
    AutomationEvent,
    AutomationRule,
    AutomationTrigger,
    automation_rule_digest,
)
from domoai.domain.models import (
    AuthorityContext,
    Command,
    ExecutionOutcome,
    ExecutionStatus,
    ExecutionSummary,
    Plan,
    PlanStatus,
    StateChangedEvent,
)


class FakeRepository:
    def __init__(self, record):
        self.record = record
        self.claimed: set[tuple[str, str]] = set()

    async def save(self, rule, consent):
        self.record = self.record.__class__(rule=rule, consent=consent)

    async def update(
        self, rule, consent, *, expected_definition_digest, expected_approval_id
    ):
        if self.record.rule.id != rule.id:
            return False
        if (
            self.record.rule.definition_digest != expected_definition_digest
            or self.record.consent.approval_id != expected_approval_id
        ):
            return False
        self.record = self.record.__class__(rule=rule, consent=consent)
        return True

    async def get(self, rule_id):
        return self.record if self.record.rule.id == rule_id else None

    async def list_all(self):
        return [self.record]

    async def set_status(self, rule_id, status):
        if self.record.rule.id != rule_id:
            return False
        self.record = self.record.__class__(
            rule=self.record.rule.model_copy(update={"status": status}),
            consent=self.record.consent,
            last_event_id=self.record.last_event_id,
            last_fired_at=self.record.last_fired_at,
        )
        return True

    async def claim_event(self, rule_id, event_id, now):
        key = (rule_id, event_id)
        if key in self.claimed:
            return False
        if self.record.last_fired_at is not None and self.record.rule.cooldown_seconds > 0:
            return False
        self.claimed.add(key)
        self.record = self.record.__class__(
            rule=self.record.rule,
            consent=self.record.consent,
            last_event_id=event_id,
            last_fired_at=now,
        )
        return True


class FakePlanService:
    def create_plan(self, plan_id, commands, *, expires_at=None):
        return Plan(id=plan_id, commands=commands, expires_at=expires_at)

    def validate(self, plan):
        return plan.model_copy(update={"status": PlanStatus.READY})


class FakeExecutor:
    def __init__(self, statuses=(ExecutionStatus.CONFIRMED_SUCCESS,)):
        self.plans = []
        self.statuses = statuses

    async def execute(self, plan):
        self.plans.append(plan)
        return ExecutionSummary(
            outcomes=[
                ExecutionOutcome(
                    plan_id=plan.id,
                    command_id=command.id,
                    execution_attempt_id=f"attempt-{index}",
                    status=status,
                )
                for index, (command, status) in enumerate(
                    zip(plan.commands, self.statuses, strict=False)
                )
            ]
        )


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, **event):
        self.events.append(event)


def _record(*, cooldown_seconds=60, authority=None, command_count=1):
    authority = authority or AuthorityContext()
    commands = [
        Command(
            id="engine-command",
            device_id="light.one",
            command="turn_on",
            idempotency_key="engine-intent",
        )
    ]
    if command_count > 1:
        commands.extend(
            Command(
                id=f"engine-command-{index}",
                device_id="light.one",
                command="turn_on",
                idempotency_key=f"engine-intent-{index}",
            )
            for index in range(2, command_count + 1)
        )
    rule = AutomationRule(
        authority=authority,
        id="rule-engine",
        name="Engine rule",
        trigger=AutomationTrigger(
            type="state_changed", device_id="sensor.one", capability="motion", expected=True
        ),
        plan_template=Plan(
            id="engine-template",
            commands=commands,
        ),
        scope="home",
        cooldown_seconds=cooldown_seconds,
        status="enabled",
    )
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    consent = AutomationConsent(
        authority=authority,
        approval_id="approval-engine",
        principal_id="operator",
        scope="home",
        rule_digest=automation_rule_digest(rule),
        approved_at=now,
        expires_at=now + timedelta(hours=1),
    )
    from domoai.persistence.repositories import AutomationRuleRecord

    return AutomationRuleRecord(rule=rule, consent=consent)


def _time_record():
    source = _record()
    rule = AutomationRule(
        id="rule-clock",
        name="Clock rule",
        trigger=AutomationTrigger(type="time", time_of_day=time(12, 0), timezone="UTC"),
        plan_template=source.rule.plan_template,
        scope="home",
        status="enabled",
    )
    now = datetime(2026, 9, 4, 11, tzinfo=UTC)
    consent = AutomationConsent(
        approval_id="approval-clock",
        principal_id="operator",
        scope="home",
        rule_digest=automation_rule_digest(rule),
        approved_at=now,
        expires_at=now + timedelta(hours=2),
    )
    from domoai.persistence.repositories import AutomationRuleRecord

    return AutomationRuleRecord(rule=rule, consent=consent)


def _updated_record(source):
    rule = AutomationRule.model_validate(
        {
            **source.rule.model_dump(mode="json"),
            "name": "Updated engine rule",
            "definition_digest": None,
        }
    )
    consent = AutomationConsent(
        approval_id="approval-engine-v2",
        principal_id="operator",
        scope=rule.scope,
        rule_digest=rule.definition_digest,
        approved_at=datetime(2026, 9, 4, 12, 1, tzinfo=UTC),
        expires_at=datetime(2026, 9, 4, 13, tzinfo=UTC),
    )
    return rule, consent


@pytest.mark.asyncio
async def test_update_requires_fresh_consent_resets_claim_and_audits_transition() -> None:
    source = _record()
    repository = FakeRepository(source)
    repository.record = source.__class__(
        rule=source.rule,
        consent=source.consent,
        last_event_id="event-before-update",
        last_fired_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )
    audit = FakeAudit()
    engine = LocalAutomationEngine(
        repository,
        FakePlanService(),
        FakeExecutor(),
        audit,
        clock=lambda: datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
    )
    updated_rule, updated_consent = _updated_record(source)

    result = await engine.update(updated_rule, updated_consent)

    assert result == updated_rule
    assert repository.record.rule.definition_digest == updated_rule.definition_digest
    assert repository.record.consent.approval_id == "approval-engine-v2"
    assert repository.record.last_event_id is None
    assert repository.record.last_fired_at is None
    transition = next(
        event for event in audit.events if event["event_type"] == "automation_rule_updated"
    )
    assert transition["payload"] == {
        "previous_definition_digest": source.rule.definition_digest,
        "definition_digest": updated_rule.definition_digest,
        "previous_consent_digest": source.consent.rule_digest,
        "consent_digest": updated_consent.rule_digest,
        "scope": updated_rule.scope,
        "status": updated_rule.status.value,
        "previous_approval_invalidated": True,
    }
    assert "approval_id" not in str(transition)


@pytest.mark.asyncio
async def test_update_rejects_reusing_previous_approval() -> None:
    source = _record()
    repository = FakeRepository(source)
    audit = FakeAudit()
    engine = LocalAutomationEngine(
        repository,
        FakePlanService(),
        FakeExecutor(),
        audit,
        clock=lambda: datetime(2026, 9, 4, 12, 2, tzinfo=UTC),
    )
    updated_rule, _ = _updated_record(source)
    reused = AutomationConsent(
        approval_id=source.consent.approval_id,
        principal_id=source.consent.principal_id,
        scope=updated_rule.scope,
        rule_digest=updated_rule.definition_digest,
        approved_at=source.consent.approved_at,
        expires_at=updated_rule.expires_at or source.consent.expires_at,
    )

    with pytest.raises(ValueError, match="fresh approval"):
        await engine.update(updated_rule, reused)


@pytest.mark.asyncio
async def test_matching_event_executes_once_and_duplicate_is_idempotent() -> None:
    repository = FakeRepository(_record())
    executor = FakeExecutor()
    audit = FakeAudit()
    clock_now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository,
        FakePlanService(),
        executor,
        audit,
        clock=lambda: clock_now,
    )
    event = AutomationEvent(
        event_id="event-1",
        event_type="state_changed",
        occurred_at=clock_now,
        device_id="sensor.one",
        capability="motion",
        value=True,
    )

    first = await engine.handle_event(event)
    second = await engine.handle_event(event)

    assert first[0].status == "executed"
    assert second[0].status in {"duplicate", "cooldown"}
    assert len(executor.plans) == 1
    assert any(event["event_type"] == "automation_fired" for event in audit.events)


@pytest.mark.asyncio
async def test_automation_preserves_authority_and_derives_one_key_per_occurrence() -> None:
    authority = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="operator",
        roles=["owner"],
        device_ids=["sensor.one", "light.one"],
        capabilities=["motion", "power"],
        operations=["turn_on"],
    )
    repository = FakeRepository(_record(cooldown_seconds=0, authority=authority))
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    for event_id in ("event-a", "event-b"):
        evaluations = await engine.handle_event(
            AutomationEvent(
                event_id=event_id,
                event_type="state_changed",
                occurred_at=now,
                device_id="sensor.one",
                capability="motion",
                value=True,
            )
        )
        assert evaluations[0].status == "executed"

    assert len(executor.plans) == 2
    assert executor.plans[0].authority.tenant_id == "tenant-a"
    assert executor.plans[0].authority.household_id == "home-a"
    assert executor.plans[0].authority.principal_id == "operator"
    assert executor.plans[0].authority.roles == ["owner"]
    assert (
        executor.plans[0].commands[0].idempotency_key
        != executor.plans[1].commands[0].idempotency_key
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "expected_status", "expected_reason"),
    [
        ((ExecutionStatus.REJECTED,), "failed", "execution_failed"),
        ((ExecutionStatus.FAILED,), "failed", "execution_failed"),
        (
            (ExecutionStatus.CONFIRMED_SUCCESS, ExecutionStatus.FAILED),
            "partial",
            "execution_partial",
        ),
        ((), "unknown", "no_execution_outcomes"),
    ],
)
async def test_automation_projects_non_success_execution_truthfully(
    statuses, expected_status, expected_reason
) -> None:
    repository = FakeRepository(
        _record(cooldown_seconds=0, command_count=max(len(statuses), 1))
    )
    executor = FakeExecutor(statuses)
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )
    evaluations = await engine.handle_event(
        AutomationEvent(
            event_id="outcome-event",
            event_type="state_changed",
            occurred_at=now,
            device_id="sensor.one",
            capability="motion",
            value=True,
        )
    )

    assert evaluations[0].status == expected_status
    assert evaluations[0].reason == expected_reason


@pytest.mark.asyncio
async def test_stale_event_is_skipped_before_execution() -> None:
    repository = FakeRepository(_record())
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )
    event = AutomationEvent(
        event_id="old-event",
        event_type="state_changed",
        occurred_at=now - timedelta(hours=1),
        device_id="sensor.one",
        capability="motion",
        value=True,
    )

    evaluations = await engine.handle_event(event)

    assert evaluations[0].status == "skipped"
    assert evaluations[0].reason == "event_stale"
    assert executor.plans == []


@pytest.mark.asyncio
async def test_expired_consent_blocks_before_claim_or_execution() -> None:
    from domoai.domain.automation import AutomationConsent
    record = _record()
    expired = record.__class__(
        rule=record.rule,
        consent=AutomationConsent(
            approval_id="approval-expired",
            principal_id="operator",
            scope=record.rule.scope,
            rule_digest=record.rule.definition_digest,
            approved_at=datetime(2026, 9, 4, 10, tzinfo=UTC),
            expires_at=datetime(2026, 9, 4, 11, tzinfo=UTC),
        ),
    )
    repository = FakeRepository(expired)
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )
    event = AutomationEvent(
        event_id="expired-event",
        event_type="state_changed",
        occurred_at=now,
        device_id="sensor.one",
        capability="motion",
        value=True,
    )

    evaluations = await engine.handle_event(event)

    assert evaluations[0].status == "blocked"
    assert evaluations[0].reason == "consent_invalid"
    assert executor.plans == []


@pytest.mark.asyncio
async def test_unavailable_state_event_is_not_a_trigger() -> None:
    repository = FakeRepository(_record())
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    evaluations = await engine.handle_state_event(
        StateChangedEvent(
            occurred_at=now,
            device_id="sensor.one",
            capability="motion",
            value=None,
            available=False,
        )
    )

    assert evaluations == []
    assert executor.plans == []


@pytest.mark.asyncio
async def test_state_event_without_current_value_is_not_a_trigger() -> None:
    repository = FakeRepository(_record())
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    evaluations = await engine.handle_state_event(
        StateChangedEvent(
            occurred_at=now,
            device_id="sensor.one",
            capability="motion",
            value=None,
        )
    )

    assert evaluations == []
    assert executor.plans == []


@pytest.mark.asyncio
async def test_time_trigger_fires_once_at_declared_local_minute() -> None:
    repository = FakeRepository(_time_record())
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    evaluations = await engine.evaluate_time(now)

    assert evaluations[0].status == "executed"
    assert len(executor.plans) == 1
