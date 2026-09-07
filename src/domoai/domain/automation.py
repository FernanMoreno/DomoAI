"""Strict, bounded domain contracts for local rule automation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from domoai.domain.models import (
    AuthorityContext,
    Plan,
    PlanStatus,
    ScalarValue,
    StrictModel,
)


class AutomationRuleStatus(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    EXPIRED = "expired"


class AutomationTrigger(StrictModel):
    type: Literal["state_changed", "time"]
    device_id: str | None = Field(default=None, min_length=1)
    capability: str | None = Field(default=None, min_length=1)
    expected: ScalarValue | None = None
    time_of_day: time | None = None
    timezone: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_shape(self) -> AutomationTrigger:
        if self.type == "state_changed":
            if self.device_id is None or self.capability is None:
                raise ValueError("state_changed trigger requires device_id and capability")
            if self.time_of_day is not None or self.timezone is not None:
                raise ValueError("state_changed trigger cannot declare a clock schedule")
        else:
            if self.time_of_day is None or self.timezone is None:
                raise ValueError("time trigger requires time_of_day and timezone")
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("time trigger timezone is unknown") from error
            if (
                self.device_id is not None
                or self.capability is not None
                or self.expected is not None
            ):
                raise ValueError("time trigger cannot declare state match fields")
        return self


class AutomationCondition(StrictModel):
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    operator: Literal["equals", "not_equals"] = "equals"
    expected: ScalarValue | None = None


class AutomationConsent(StrictModel):
    authority: AuthorityContext = Field(default_factory=AuthorityContext)
    approval_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    scope: str = Field(min_length=1, max_length=200)
    rule_digest: str = Field(min_length=1, pattern=r"^sha256:[0-9a-f]{64}$")
    approved_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> AutomationConsent:
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("automation consent approved_at must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("automation consent expires_at must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("automation consent must expire after approval")
        return self


class AutomationRule(StrictModel):
    authority: AuthorityContext = Field(default_factory=AuthorityContext)
    schema_version: str = "v1"
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    trigger: AutomationTrigger
    conditions: list[AutomationCondition] = Field(default_factory=list, max_length=4)
    plan_template: Plan
    scope: str = Field(min_length=1, max_length=200)
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)
    expires_at: datetime | None = None
    status: AutomationRuleStatus = AutomationRuleStatus.DISABLED
    definition_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_template(self) -> AutomationRule:
        template = self.plan_template
        if len(template.commands) > 10:
            raise ValueError("automation action plan is limited to 10 commands")
        if (
            template.execute_at is not None
            or template.approval is not None
            or template.execution is not None
            or template.validation is not None
            or template.status is not PlanStatus.DRAFT
        ):
            raise ValueError("automation plan_template must be an unvalidated draft")
        for field_name, value in (("expires_at", self.expires_at),):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"automation {field_name} must be timezone-aware")
        expected_digest = automation_rule_digest(self)
        if self.definition_digest is None:
            object.__setattr__(self, "definition_digest", expected_digest)
        elif self.definition_digest != expected_digest:
            raise ValueError("automation rule definition_digest does not match definition")
        return self


class AutomationEvent(StrictModel):
    event_id: str = Field(min_length=1, max_length=200)
    event_type: Literal["state_changed", "time"]
    occurred_at: datetime
    device_id: str | None = Field(default=None, min_length=1)
    capability: str | None = Field(default=None, min_length=1)
    value: ScalarValue | None = None

    @model_validator(mode="after")
    def validate_event(self) -> AutomationEvent:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("automation event occurred_at must be timezone-aware")
        if self.event_type == "state_changed" and (
            self.device_id is None or self.capability is None
        ):
            raise ValueError("state_changed event requires device_id and capability")
        return self


AutomationEvaluationStatus = Literal[
    "executed",
    "failed",
    "partial",
    "skipped",
    "blocked",
    "expired",
    "duplicate",
    "cooldown",
    "unknown",
]


class AutomationEvaluation(StrictModel):
    rule_id: str
    event_id: str
    status: AutomationEvaluationStatus
    reason: str = Field(min_length=1, max_length=200)
    plan_id: str | None = None
    observed_at: datetime


def automation_rule_digest(rule: AutomationRule) -> str:
    """Hash only rule definition fields, excluding lifecycle and its digest."""

    payload = rule.model_dump(mode="json", exclude={"status", "definition_digest", "plan_template"})
    payload["plan_template"] = {
        "schema_version": rule.plan_template.schema_version,
        "commands": [command.model_dump(mode="json") for command in rule.plan_template.commands],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

__all__ = [
    "AutomationCondition",
    "AutomationConsent",
    "AutomationEvent",
    "AutomationEvaluation",
    "AutomationEvaluationStatus",
    "AutomationRule",
    "AutomationRuleStatus",
    "AutomationTrigger",
    "automation_rule_digest",
]
