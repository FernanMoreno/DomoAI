"""Strict review-only contracts. A view or decision is never execution authority."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from domoai.domain.automation import AutomationCondition, AutomationTrigger
from domoai.domain.household_preferences import PreferenceVersion

type Identifier = Annotated[str, Field(min_length=1, max_length=200)]
type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type TargetKind = Literal["plan", "bundle", "recurrence", "rule"]


def _offset_datetime(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("an explicit datetime with offset is required")
    return value


type OffsetTimestamp = Annotated[datetime, BeforeValidator(_offset_datetime)]
type Timestamp = Annotated[OffsetTimestamp, AfterValidator(lambda value: value.astimezone(UTC))]


class ConsentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class ConsentControlVersion(PreferenceVersion):
    """Consent generation/revision, distinct from preference authority."""


class ConsentReference(ConsentModel):
    kind: TargetKind
    id: Identifier


class ConsentWindow(ConsentModel):
    not_before: OffsetTimestamp
    not_after: OffsetTimestamp
    timezone: Annotated[str, Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def concrete_window(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be an IANA zone") from error
        for value in (self.not_before, self.not_after):
            # Even a zero-offset native zone can contain a gap. Validate its
            # civil time before treating a zero-offset instant as canonical UTC.
            instant = value.astimezone(UTC)
            round_trip = instant.astimezone(value.tzinfo)
            if round_trip.utcoffset() != value.utcoffset() or round_trip.replace(
                tzinfo=None
            ) != value.replace(tzinfo=None):
                raise ValueError("datetime does not exist in its timezone")
            if value.utcoffset() != UTC.utcoffset(value):
                local = instant.astimezone(zone)
                if local.utcoffset() != value.utcoffset() or local.replace(
                    tzinfo=None
                ) != value.replace(tzinfo=None):
                    raise ValueError("datetime does not match timezone/offset")
        start = self.not_before.astimezone(UTC)
        end = self.not_after.astimezone(UTC)
        if end <= start:
            raise ValueError("not_after must follow not_before")
        object.__setattr__(self, "not_before", start)
        object.__setattr__(self, "not_after", end)
        return self


class ConsentHorizon(ConsentModel):
    start: Timestamp
    end: Timestamp

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("horizon end must follow start")
        return self


class ConsentBindings(ConsentModel):
    # Nullable is not optional: the wire carries every binding explicitly.
    validation_digest: Digest | None
    window_digest: Digest | None
    bundle_digest: Digest | None
    recurrence_digest: Digest | None
    rule_digest: Digest | None
    schedule_revision: int = Field(ge=0)
    preference_version: PreferenceVersion
    preference_observed_at: Timestamp
    preference_next_transition_at: Timestamp | None
    preference_horizon: ConsentHorizon
    control_version: ConsentControlVersion


class ConsentAction(ConsentModel):
    command_id: Identifier
    device_id: Identifier
    device_name: Annotated[str, Field(min_length=1, max_length=500)]
    command: Identifier
    value: bool | int | float | str | None
    unit: Identifier | None
    risk_class: Literal["safe", "confirm", "restricted"]


def _rule_fields(value: object) -> object:
    if isinstance(value, (AutomationTrigger, AutomationCondition)):
        value = value.model_dump()
    if isinstance(value, dict):
        expected = value.get("expected")
        if isinstance(expected, float) and not math.isfinite(expected):
            raise ValueError("rule expected value must be finite")
    return value


class ConsentConstraints(ConsentModel):
    scope: Identifier
    trigger: AutomationTrigger
    conditions: list[AutomationCondition] = Field(max_length=4)
    cooldown_seconds: int = Field(ge=0, le=86400)

    @field_validator("trigger", mode="before")
    @classmethod
    def strict_trigger(cls, value: object) -> AutomationTrigger:
        value = _rule_fields(value)
        if isinstance(value, dict):
            value = value.copy()
            # Explicit textual time is the wire representation; a numeric
            # duration or bytes must not be coerced by the legacy model.
            clock_time = value.get("time_of_day")
            if isinstance(clock_time, str):
                value["time_of_day"] = time.fromisoformat(clock_time)
        return AutomationTrigger.model_validate(value, strict=True)

    @field_validator("conditions", mode="before")
    @classmethod
    def strict_conditions(cls, value: object) -> object:
        if isinstance(value, list):
            return [
                AutomationCondition.model_validate(_rule_fields(item), strict=True)
                for item in value
            ]
        return value


class ConsentRisk(ConsentModel):
    policy_id: Identifier | None
    action: Literal["allow", "deny", "confirm"]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    allows_stale: bool


class ConsentEvidence(ConsentModel):
    kind: Literal["validation", "rule"]
    id: Identifier
    digest: Digest


_TARGET_DIGESTS = {
    "plan": "validation_digest",
    "bundle": "bundle_digest",
    "recurrence": "recurrence_digest",
    "rule": "rule_digest",
}


def _target_schema(schema: dict[str, JsonValue]) -> None:
    clauses: list[JsonValue] = []
    for kind, digest in _TARGET_DIGESTS.items():
        clauses.append(
            {
                "if": {"properties": {"reference": {"properties": {"kind": {"const": kind}}}}},
                "then": {"properties": {"bindings": {"properties": {digest: {"type": "string"}}}}},
            }
        )
    clauses.append(
        {
            "if": {"properties": {"reference": {"properties": {"kind": {"const": "rule"}}}}},
            "then": {
                "properties": {"actions": {"maxItems": 10}, "constraints": {"type": "object"}}
            },
        }
    )
    schema["allOf"] = clauses


class ConsentView(ConsentModel):
    model_config = ConfigDict(json_schema_extra=_target_schema)
    schema_version: Literal["domoai.household-consent.v1"]
    reference: ConsentReference
    tenant_id: Identifier
    household_id: Identifier
    actions: list[ConsentAction] = Field(min_length=1, max_length=50)
    window: ConsentWindow
    duration_seconds: int = Field(gt=0)
    expires_at: Timestamp
    constraints: ConsentConstraints | None
    risks: list[ConsentRisk]
    evidence_refs: list[ConsentEvidence]
    bindings: ConsentBindings
    permission_status: Literal[
        "awaiting_review",
        "active",
        "expired",
        "revocation_pending",
        "revoked",
        "superseded",
        "unavailable",
    ]
    physical_effect_refs: list[Identifier]

    @model_validator(mode="after")
    def complete_target(self) -> Self:
        if getattr(self.bindings, _TARGET_DIGESTS[self.reference.kind]) is None:
            raise ValueError("the exact target digest is required")
        if self.reference.kind == "rule" and (self.constraints is None or len(self.actions) > 10):
            raise ValueError("rule review requires bounded constraints and at most 10 actions")
        return self


class ConsentDecision(ConsentModel):
    challenge_id: Identifier
    action: Literal["approve", "revoke", "reject"]
    expected_review_digest: Digest
    request_id: Identifier
