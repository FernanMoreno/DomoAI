from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

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


class MultiRuleFakeRepository:
    def __init__(self, records):
        self.records = {record.rule.id: record for record in records}
        self.claimed: set[tuple[str, str]] = set()

    async def list_all(self):
        return list(self.records.values())

    async def claim_event(self, rule_id, event_id, now):
        key = (rule_id, event_id)
        if key in self.claimed:
            return False
        record = self.records[rule_id]
        self.claimed.add(key)
        self.records[rule_id] = record.__class__(
            rule=record.rule,
            consent=record.consent,
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


def _time_record(
    *, rule_id="rule-clock", hour=12, timezone="UTC", authority=None, consent_at=None
):
    source = _record()
    authority = authority or source.rule.authority
    rule = AutomationRule(
        authority=authority,
        id=rule_id,
        name="Clock rule",
        trigger=AutomationTrigger(type="time", time_of_day=time(hour, 0), timezone=timezone),
        plan_template=source.rule.plan_template,
        scope="home",
        status="enabled",
    )
    now = consent_at or datetime(2026, 9, 4, 11, tzinfo=UTC)
    consent = AutomationConsent(
        authority=authority,
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


def _state_record(rule_id):
    source = _record(cooldown_seconds=0)
    rule = AutomationRule.model_validate(
        {**source.rule.model_dump(mode="json"), "id": rule_id, "definition_digest": None}
    )
    consent = AutomationConsent(
        authority=rule.authority,
        approval_id=f"approval-{rule_id}",
        principal_id="operator",
        scope=rule.scope,
        rule_digest=rule.definition_digest,
        approved_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
        expires_at=datetime(2026, 9, 4, 13, tzinfo=UTC),
    )
    return source.__class__(rule=rule, consent=consent)


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
async def test_each_rule_rechecks_event_freshness_after_a_slow_prior_execution() -> None:
    clock = [datetime(2026, 9, 4, 12, tzinfo=UTC)]

    class AdvancingExecutor(FakeExecutor):
        async def execute(self, plan):
            result = await super().execute(plan)
            clock[0] = clock[0] + timedelta(minutes=6)
            return result

    executor = AdvancingExecutor()
    engine = LocalAutomationEngine(
        MultiRuleFakeRepository([_state_record("rule-first"), _state_record("rule-second")]),
        FakePlanService(),
        executor,
        FakeAudit(),
        clock=lambda: clock[0],
    )

    evaluations = await engine.handle_event(
        AutomationEvent(
            event_id="slow-state-event",
            event_type="state_changed",
            occurred_at=clock[0],
            device_id="sensor.one",
            capability="motion",
            value=True,
        )
    )

    assert [(item.rule_id, item.status, item.reason) for item in evaluations] == [
        ("rule-first", "executed", "plan_executed"),
        ("rule-second", "skipped", "event_stale"),
    ]
    assert len(executor.plans) == 1


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


@pytest.mark.asyncio
async def test_time_evaluation_rejects_a_naive_clock_input() -> None:
    engine = LocalAutomationEngine(
        MultiRuleFakeRepository([_time_record()]),
        FakePlanService(),
        FakeExecutor(),
        FakeAudit(),
        clock=lambda: datetime(2026, 9, 4, 12, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await engine.evaluate_time(datetime(2026, 9, 4, 12))


@pytest.mark.asyncio
async def test_time_evaluation_targets_only_the_rule_at_its_declared_local_minute() -> None:
    repository = MultiRuleFakeRepository(
        [_time_record(rule_id="rule-noon", hour=12), _time_record(rule_id="rule-evening", hour=18)]
    )
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    evaluations = await engine.evaluate_time(now)

    assert [(item.rule_id, item.status) for item in evaluations] == [("rule-noon", "executed")]
    assert [plan.id for plan in executor.plans] == [
        "automation:rule-noon:time:rule-noon:2026-09-04T12:00"
    ]


@pytest.mark.asyncio
async def test_time_event_uses_canonical_targeted_occurrence_identity() -> None:
    repository = MultiRuleFakeRepository([_time_record(rule_id="rule-noon", hour=12)])
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    first = await engine.handle_event(
        AutomationEvent(
            event_id="caller-retry-a",
            event_type="time",
            target_rule_id="rule-noon",
            occurred_at=now,
        )
    )
    second = await engine.handle_event(
        AutomationEvent(
            event_id="caller-retry-b",
            event_type="time",
            target_rule_id="rule-noon",
            occurred_at=now,
        )
    )

    assert first[0].event_id == "time:rule-noon:2026-09-04T12:00"
    assert second[0].status == "duplicate"
    assert len(executor.plans) == 1


@pytest.mark.asyncio
async def test_time_event_rejects_a_targeted_rule_when_its_local_time_does_not_match() -> None:
    repository = MultiRuleFakeRepository([_time_record(rule_id="rule-evening", hour=18)])
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    evaluations = await engine.handle_event(
        AutomationEvent(
            event_id="caller-noon",
            event_type="time",
            target_rule_id="rule-evening",
            occurred_at=now,
        )
    )

    assert evaluations == []
    assert executor.plans == []


@pytest.mark.asyncio
async def test_same_utc_minute_keeps_distinct_rules_and_households_isolated() -> None:
    home_a = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="operator",
        roles=["owner"],
        device_ids=["light.one"],
        capabilities=["power"],
        operations=["turn_on"],
    )
    home_b = home_a.model_copy(
        update={"tenant_id": "tenant-b", "household_id": "home-b", "household_ids": ["home-b"]}
    )
    repository = MultiRuleFakeRepository(
        [
            _time_record(rule_id="rule-home-a", hour=12, authority=home_a),
            _time_record(
                rule_id="rule-home-b", hour=14, timezone="Europe/Madrid", authority=home_b
            ),
        ]
    )
    executor = FakeExecutor()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    evaluations = await engine.evaluate_time(now)

    assert {item.rule_id for item in evaluations} == {"rule-home-a", "rule-home-b"}
    assert {plan.authority.household_id for plan in executor.plans} == {"home-a", "home-b"}
    assert (
        executor.plans[0].commands[0].idempotency_key
        != executor.plans[1].commands[0].idempotency_key
    )


@pytest.mark.asyncio
async def test_repeated_dst_local_minute_has_one_occurrence_per_utc_instant() -> None:
    repository = MultiRuleFakeRepository(
        [
            _time_record(
                rule_id="rule-fold",
                hour=2,
                timezone="Europe/Madrid",
                consent_at=datetime(2026, 10, 25, 0, tzinfo=UTC),
            )
        ]
    )
    executor = FakeExecutor()
    first_fold = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    second_fold = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    clock = [first_fold]
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: clock[0]
    )

    first = await engine.evaluate_time(first_fold)
    clock[0] = second_fold
    second = await engine.evaluate_time(second_fold)

    assert [item.event_id for item in first + second] == [
        "time:rule-fold:2026-10-25T00:00",
        "time:rule-fold:2026-10-25T01:00",
    ]
    assert len(executor.plans) == 2


@pytest.mark.asyncio
async def test_dst_fold_uses_utc_instants_for_future_and_stale_time_events() -> None:
    zone = ZoneInfo("Europe/Madrid")
    first_fold = datetime(2026, 10, 25, 2, tzinfo=zone, fold=0)
    second_fold = datetime(2026, 10, 25, 2, tzinfo=zone, fold=1)
    record = _time_record(
        rule_id="rule-fold-window",
        hour=2,
        timezone="Europe/Madrid",
        consent_at=first_fold.astimezone(UTC),
    )
    future_engine = LocalAutomationEngine(
        MultiRuleFakeRepository([record]),
        FakePlanService(),
        FakeExecutor(),
        FakeAudit(),
        clock=lambda: first_fold,
    )
    stale_engine = LocalAutomationEngine(
        MultiRuleFakeRepository([record]),
        FakePlanService(),
        FakeExecutor(),
        FakeAudit(),
        clock=lambda: second_fold,
    )

    future = await future_engine.handle_event(
        AutomationEvent(
            event_id="future-fold",
            event_type="time",
            target_rule_id="rule-fold-window",
            occurred_at=second_fold,
        )
    )
    stale = await stale_engine.handle_event(
        AutomationEvent(
            event_id="stale-fold",
            event_type="time",
            target_rule_id="rule-fold-window",
            occurred_at=first_fold,
        )
    )

    assert [(item.status, item.reason) for item in future] == [("skipped", "event_stale")]
    assert [(item.status, item.reason) for item in stale] == [("skipped", "event_stale")]


@pytest.mark.asyncio
async def test_nonexistent_dst_local_minute_is_not_recovered_late() -> None:
    repository = MultiRuleFakeRepository(
        [
            _time_record(
                rule_id="rule-gap",
                hour=2,
                timezone="Europe/Madrid",
                consent_at=datetime(2026, 3, 29, 0, tzinfo=UTC),
            )
        ]
    )
    executor = FakeExecutor()
    now = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: now
    )

    evaluations = await engine.evaluate_time(now)

    assert evaluations == []
    assert executor.plans == []


@pytest.mark.asyncio
async def test_imaginary_dst_event_does_not_match_the_missing_local_minute() -> None:
    zone = ZoneInfo("Europe/Madrid")
    record = _time_record(
        rule_id="rule-imaginary-gap",
        hour=2,
        timezone="Europe/Madrid",
        consent_at=datetime(2026, 3, 29, 0, tzinfo=UTC),
    )
    executor = FakeExecutor()
    engine = LocalAutomationEngine(
        MultiRuleFakeRepository([record]),
        FakePlanService(),
        executor,
        FakeAudit(),
        clock=lambda: datetime(2026, 3, 29, 1, tzinfo=UTC),
    )

    evaluations = await engine.handle_event(
        AutomationEvent(
            event_id="imaginary-gap",
            event_type="time",
            target_rule_id="rule-imaginary-gap",
            occurred_at=datetime(2026, 3, 29, 2, tzinfo=zone),
        )
    )

    assert evaluations == []
    assert executor.plans == []


@pytest.mark.asyncio
async def test_disabled_or_stale_time_occurrences_do_not_execute() -> None:
    current = datetime(2026, 9, 4, 12, tzinfo=UTC)
    disabled = _time_record(rule_id="rule-disabled", hour=12)
    disabled = disabled.__class__(
        rule=disabled.rule.model_copy(update={"status": "disabled"}), consent=disabled.consent
    )
    repository = MultiRuleFakeRepository([disabled])
    executor = FakeExecutor()
    engine = LocalAutomationEngine(
        repository, FakePlanService(), executor, FakeAudit(), clock=lambda: current
    )

    disabled_result = await engine.evaluate_time(current)
    stale_repository = MultiRuleFakeRepository([_time_record(rule_id="rule-stale", hour=12)])
    stale_engine = LocalAutomationEngine(
        stale_repository, FakePlanService(), executor, FakeAudit(), clock=lambda: current
    )
    stale_result = await stale_engine.handle_event(
        AutomationEvent(
            event_id="caller-stale",
            event_type="time",
            target_rule_id="rule-stale",
            occurred_at=current - timedelta(minutes=6),
        )
    )

    assert disabled_result == []
    assert [(item.rule_id, item.reason) for item in stale_result] == [
        ("rule-stale", "event_stale")
    ]
    assert executor.plans == []
