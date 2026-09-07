"""Deterministic, bounded local automation without an LLM or network."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from domoai.application.execution_projection import project_execution_summary
from domoai.application.recurrence import occurrence_idempotency_key
from domoai.domain.automation import (
    AutomationCondition,
    AutomationConsent,
    AutomationEvaluation,
    AutomationEvaluationStatus,
    AutomationEvent,
    AutomationRule,
    AutomationRuleStatus,
    automation_rule_digest,
)
from domoai.domain.errors import DomainError
from domoai.domain.models import (
    PlanStatus,
    StateChangedEvent,
    StateStatus,
)
from domoai.persistence.repositories import AutomationRuleRecord, AutomationRuleRepository
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.state_store import StateStore


class LocalAutomationEngine:
    """Evaluate persisted rules and submit only fresh validated plans."""

    def __init__(
        self,
        repository: AutomationRuleRepository,
        plan_service: Any,
        executor: Any,
        audit: AuditLog,
        *,
        plan_repository: Any | None = None,
        state_store: StateStore | None = None,
        clock: Clock | Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.plan_service = plan_service
        self.executor = executor
        self.audit = audit
        self.plan_repository = plan_repository
        self.state_store = state_store
        self._clock = clock or SystemClock()

    def _now(self) -> datetime:
        return self._clock() if callable(self._clock) else self._clock.now()

    async def register(
        self, rule: AutomationRule, consent: AutomationConsent
    ) -> AutomationRule:
        self._assert_consent(rule, consent)
        if consent.expires_at <= self._now():
            raise ValueError("automation consent has expired")
        await self.repository.save(rule, consent)
        self.audit.append(
            event_type="automation_rule_created",
            actor=consent.principal_id,
            subject_id=rule.id,
            payload={
                "rule_digest": rule.definition_digest,
                "scope": rule.scope,
                "status": rule.status.value,
            },
        )
        return rule

    async def update(
        self, rule: AutomationRule, consent: AutomationConsent
    ) -> AutomationRule:
        """Install a new rule version only with a fresh standing consent."""

        record = await self.repository.get(rule.id)
        if record is None:
            raise ValueError("automation rule does not exist")
        self._assert_consent(rule, consent)
        if consent.expires_at <= self._now():
            raise ValueError("automation consent has expired")
        if consent.approval_id == record.consent.approval_id:
            raise ValueError("automation rule update requires a fresh approval")
        previous_definition_digest = record.rule.definition_digest
        if previous_definition_digest is None:
            raise ValueError("stored automation rule has no definition digest")
        updated = await self.repository.update(
            rule,
            consent,
            expected_definition_digest=previous_definition_digest,
            expected_approval_id=record.consent.approval_id,
        )
        if not updated:
            raise ValueError("automation rule update lost its target")
        self.audit.append(
            event_type="automation_rule_updated",
            actor=consent.principal_id,
            subject_id=rule.id,
            payload={
                "previous_definition_digest": record.rule.definition_digest,
                "definition_digest": rule.definition_digest,
                "previous_consent_digest": record.consent.rule_digest,
                "consent_digest": consent.rule_digest,
                "scope": rule.scope,
                "status": rule.status.value,
                "previous_approval_invalidated": True,
            },
        )
        return rule

    async def set_status(self, rule_id: str, status: AutomationRuleStatus) -> bool:
        record = await self.repository.get(rule_id)
        if record is None:
            return False
        if status is AutomationRuleStatus.ENABLED:
            self._assert_consent(record.rule, record.consent)
            if record.consent.expires_at <= self._now():
                await self.repository.set_status(rule_id, AutomationRuleStatus.EXPIRED)
                return False
        changed = await self.repository.set_status(rule_id, status)
        if changed:
            self.audit.append(
                event_type=f"automation_rule_{status.value}",
                actor="runtime",
                subject_id=rule_id,
                payload={"status": status.value},
            )
        return changed

    async def list_rules(self) -> list[AutomationRuleRecord]:
        return await self.repository.list_all()

    async def handle_state_event(self, event: StateChangedEvent) -> list[AutomationEvaluation]:
        if (
            event.occurred_at is None
            or event.device_id is None
            or event.capability is None
            or event.available is False
            or event.value is None
        ):
            return []
        return await self.handle_event(
            AutomationEvent(
                event_id=self._source_event_id(event),
                event_type="state_changed",
                occurred_at=event.occurred_at,
                device_id=event.device_id,
                capability=event.capability,
                value=event.value,
            )
        )

    async def evaluate_time(self, now: datetime | None = None) -> list[AutomationEvaluation]:
        current = now or self._now()
        records = await self.repository.list_all()
        evaluations: list[AutomationEvaluation] = []
        for record in records:
            trigger = record.rule.trigger
            if (
                record.rule.status is not AutomationRuleStatus.ENABLED
                or trigger.type != "time"
                or trigger.time_of_day is None
                or trigger.timezone is None
            ):
                continue
            local = current.astimezone(ZoneInfo(trigger.timezone))
            if local.hour != trigger.time_of_day.hour or local.minute != trigger.time_of_day.minute:
                continue
            event = AutomationEvent(
                event_id=f"time:{record.rule.id}:{current.astimezone(UTC).strftime('%Y-%m-%dT%H:%M')}",
                event_type="time",
                occurred_at=current,
            )
            evaluations.extend(await self.handle_event(event))
        return evaluations

    async def handle_event(self, event: AutomationEvent) -> list[AutomationEvaluation]:
        evaluations: list[AutomationEvaluation] = []
        for record in await self.repository.list_all():
            if record.rule.status is not AutomationRuleStatus.ENABLED:
                continue
            stale_after = (
                self.state_store.stale_after
                if self.state_store is not None
                else timedelta(minutes=5)
            )
            if event.occurred_at > self._now() or self._now() - event.occurred_at > stale_after:
                evaluation = self._evaluation(record.rule, event, "skipped", "event_stale")
                evaluations.append(evaluation)
                self._audit_evaluation(evaluation)
                continue
            if record.rule.expires_at is not None and self._now() >= record.rule.expires_at:
                await self.repository.set_status(record.rule.id, AutomationRuleStatus.EXPIRED)
                evaluations.append(self._evaluation(record.rule, event, "expired", "rule_expired"))
                self._audit_evaluation(evaluations[-1])
                continue
            if not self._matches_trigger(record.rule, event):
                continue
            if not await self._conditions_match(record.rule.conditions, event):
                evaluation = self._evaluation(record.rule, event, "skipped", "condition_not_met")
                evaluations.append(evaluation)
                self._audit_evaluation(evaluation)
                continue
            if not self._consent_is_current(record.rule, record.consent):
                evaluation = self._evaluation(record.rule, event, "blocked", "consent_invalid")
                evaluations.append(evaluation)
                self._audit_evaluation(evaluation)
                continue
            claimed = await self.repository.claim_event(
                record.rule.id, event.event_id, self._now()
            )
            if not claimed:
                reason = (
                    "duplicate_event"
                    if record.last_event_id == event.event_id
                    else "cooldown_active"
                )
                status = "duplicate" if reason == "duplicate_event" else "cooldown"
                evaluation = self._evaluation(
                    record.rule,
                    event,
                    cast(AutomationEvaluationStatus, status),
                    reason,
                )
                evaluations.append(evaluation)
                self._audit_evaluation(evaluation)
                continue
            evaluation = await self._execute_claimed(record.rule, record.consent, event)
            evaluations.append(evaluation)
            self._audit_evaluation(evaluation)
        return evaluations

    async def _execute_claimed(
        self, rule: AutomationRule, consent: AutomationConsent, event: AutomationEvent
    ) -> AutomationEvaluation:
        plan_id = f"automation:{rule.id}:{event.event_id}"
        status: AutomationEvaluationStatus = "unknown"
        try:
            authority = rule.authority.model_copy(
                update={"principal_id": consent.principal_id}
            )
            commands = [
                command.model_copy(
                    update={
                        "idempotency_key": occurrence_idempotency_key(
                            household_id=authority.household_id,
                            automation_id=rule.id,
                            definition_digest=(
                                rule.definition_digest or automation_rule_digest(rule)
                            ),
                            occurrence_id=event.event_id,
                            command_id=command.id,
                            command_index=index,
                        )
                    }
                )
                for index, command in enumerate(rule.plan_template.commands)
            ]
            plan = self.plan_service.create_plan(
                plan_id,
                commands,
                expires_at=rule.expires_at,
            )
            plan = plan.model_copy(
                update={
                    "authority": authority,
                    "agent_request_id": consent.principal_id,
                }
            )
            validated = self.plan_service.validate(plan)
            if self.plan_repository is not None:
                await self.plan_repository.save(validated)
            if validated.status is PlanStatus.READY:
                execution = await self.executor.execute(validated)
                projected_status, reason = project_execution_summary(execution)
                status = cast(AutomationEvaluationStatus, projected_status)
            elif validated.status is PlanStatus.REQUIRES_CONFIRMATION:
                status, reason = "blocked", "confirmation_required"
            else:
                status, reason = "blocked", "plan_validation_failed"
        except (DomainError, ValueError, RuntimeError) as error:
            status, reason = "unknown", self._safe_reason(error)
        return self._evaluation(
            rule,
            event,
            status,
            reason,
            plan_id=plan_id,
        )

    async def _conditions_match(
        self, conditions: list[AutomationCondition], event: AutomationEvent
    ) -> bool:
        for condition in conditions:
            if (
                condition.device_id == event.device_id
                and condition.capability == event.capability
            ):
                value = event.value
            elif self.state_store is not None:
                snapshot = self.state_store.peek(condition.device_id, condition.capability)
                if snapshot is None:
                    return False
                effective = self.state_store.effective_snapshot(snapshot, self._now())
                if effective.status is not StateStatus.CURRENT:
                    return False
                value = effective.value
            else:
                return False
            matches = value == condition.expected
            if condition.operator == "not_equals":
                matches = not matches
            if not matches:
                return False
        return True

    @staticmethod
    def _matches_trigger(rule: AutomationRule, event: AutomationEvent) -> bool:
        trigger = rule.trigger
        if trigger.type == "time":
            return event.event_type == "time"
        return (
            event.event_type == "state_changed"
            and trigger.device_id == event.device_id
            and trigger.capability == event.capability
            and (trigger.expected is None or trigger.expected == event.value)
        )

    @staticmethod
    def _assert_consent(rule: AutomationRule, consent: AutomationConsent) -> None:
        if consent.rule_digest != automation_rule_digest(rule) or consent.scope != rule.scope:
            raise ValueError("automation consent does not match rule")
        expected_authority = rule.authority.model_copy(
            update={"principal_id": consent.principal_id}
        )
        consent_authority = consent.authority.model_copy(
            update={"principal_id": consent.principal_id}
        )
        if consent_authority != expected_authority:
            raise ValueError("automation consent authority does not match rule")

    def _consent_is_current(self, rule: AutomationRule, consent: AutomationConsent) -> bool:
        expected_authority = rule.authority.model_copy(
            update={"principal_id": consent.principal_id}
        )
        consent_authority = consent.authority.model_copy(
            update={"principal_id": consent.principal_id}
        )
        return (
            consent.expires_at > self._now()
            and consent.rule_digest == rule.definition_digest
            and consent.scope == rule.scope
            and consent_authority == expected_authority
        )

    def _evaluation(
        self,
        rule: AutomationRule,
        event: AutomationEvent,
        status: AutomationEvaluationStatus,
        reason: str,
        *,
        plan_id: str | None = None,
    ) -> AutomationEvaluation:
        return AutomationEvaluation(
            rule_id=rule.id,
            event_id=event.event_id,
            status=status,
            reason=reason[:200],
            plan_id=plan_id,
            observed_at=self._now(),
        )

    def _audit_evaluation(self, evaluation: AutomationEvaluation) -> None:
        event_type = (
            "automation_fired"
            if evaluation.status == "executed"
            else f"automation_{evaluation.status}"
        )
        self.audit.append(
            event_type=event_type,
            actor="runtime",
            subject_id=evaluation.rule_id,
            payload={
                "event_id": evaluation.event_id,
                "reason": evaluation.reason,
                "plan_id": evaluation.plan_id,
            },
        )

    @staticmethod
    def _source_event_id(event: StateChangedEvent) -> str:
        cursor = event.source_cursor
        if cursor is not None:
            return f"{cursor.source_id}:{cursor.stream_id}:{cursor.epoch}:{cursor.sequence}"
        return ":".join(
            str(item)
            for item in (event.source_adapter_id, event.external_id, event.occurred_at)
        )

    @staticmethod
    def _safe_reason(error: BaseException) -> str:
        text = str(error).strip().replace("\n", " ")
        return text[:200] or "execution_failed"
