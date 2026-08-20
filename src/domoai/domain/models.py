"""Canonical, transport-independent DomoAI models."""

from __future__ import annotations

from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "v1"
type ScalarValue = bool | int | float | str


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class DeviceType(StrEnum):
    LIGHT = "light"
    SWITCH = "switch"
    COVER = "cover"
    CLIMATE = "climate"
    SENSOR = "sensor"
    ENERGY = "energy"
    EV_CHARGER = "ev_charger"
    UNSUPPORTED = "unsupported"


class AvailabilityStatus(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class StateStatus(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class CapabilityKind(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    ENUM = "enum"
    TEXT = "text"
    TIMESTAMP = "timestamp"


class RiskClass(StrEnum):
    SAFE = "safe"
    CONFIRM = "confirm"
    RESTRICTED = "restricted"


class PolicyAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    READY = "ready"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    STALE = "stale"
    REQUIRES_CONFIRMATION = "requires_confirmation"


class ExecutionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONFIRMED_SUCCESS = "confirmed_success"
    UNKNOWN = "unknown"


class SourceRef(StrictModel):
    adapter_id: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    external_type: str | None = None
    metadata_digest: str | None = None


class Area(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    parent_id: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class Capability(StrictModel):
    name: str = Field(min_length=1)
    kind: CapabilityKind
    unit: str | None = None
    readable: bool
    writable: bool
    minimum: float | int | None = None
    maximum: float | int | None = None
    enum_values: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_value_domain(self) -> Capability:
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("minimum and maximum must be provided together")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must be less than or equal to maximum")
        if self.kind not in {CapabilityKind.INTEGER, CapabilityKind.NUMBER}:
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("numeric bounds are only valid for integer or number capabilities")
        if self.kind is CapabilityKind.ENUM and not self.enum_values:
            raise ValueError("enum capabilities require enum_values")
        return self


class Device(StrictModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    schema_version: str = SCHEMA_VERSION
    type: DeviceType
    name: str = Field(min_length=1)
    area_id: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    protocol: str = Field(min_length=1)
    capabilities: list[Capability] = Field(default_factory=list)
    availability: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    source_refs: list[SourceRef] = Field(min_length=1)
    last_seen_at: datetime | None = None

    @model_validator(mode="after")
    def validate_source_refs(self) -> Device:
        identities = {(ref.adapter_id, ref.external_id) for ref in self.source_refs}
        if len(identities) != len(self.source_refs):
            raise ValueError("source_refs must be unique")
        return self


class StateSnapshot(StrictModel):
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    value: ScalarValue | None
    unit: str | None = None
    observed_at: datetime
    received_at: datetime
    status: StateStatus
    source_ref: SourceRef

    @model_validator(mode="after")
    def validate_timestamps(self) -> StateSnapshot:
        if self.observed_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("observed_at and received_at must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("received_at must be greater than or equal to observed_at")
        return self


class Precondition(StrictModel):
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    expected: ScalarValue | None


class SafetyLimit(StrictModel):
    device_type: DeviceType
    capability: str = Field(min_length=1)
    minimum: float | int | None = None
    maximum: float | int | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> SafetyLimit:
        if self.minimum is None and self.maximum is None:
            raise ValueError("safety limit must set minimum, maximum, or both")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class Command(StrictModel):
    id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    value: ScalarValue | None = None
    unit: str | None = None
    preconditions: list[Precondition] = Field(default_factory=list)
    risk_class: RiskClass = RiskClass.SAFE
    idempotency_key: str = Field(min_length=1)
    intent: str | None = None


class ErrorDetail(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    field: str | None = None
    device_id: str | None = None
    capability: str | None = None
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class PlanDependencies(StrictModel):
    inventory_revision: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    state_versions: dict[str, int] = Field(default_factory=dict)


class ValidationResult(StrictModel):
    status: ValidationStatus
    validated_at: datetime
    runtime_revision: str = Field(min_length=1)
    errors: list[ErrorDetail] = Field(default_factory=list)
    digest: str = Field(min_length=1)
    dependencies: PlanDependencies | None = None


class Policy(StrictModel):
    id: str = Field(min_length=1)
    target: dict[str, Any] = Field(default_factory=dict)
    action: PolicyAction
    conditions: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    enabled: bool = True


class PolicyDecision(StrictModel):
    policy_id: str | None = None
    action: PolicyAction
    reason: str = Field(min_length=1)


class Approval(StrictModel):
    status: str = Field(pattern=r"^(approved|denied|expired)$")
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    validation_digest: str = Field(min_length=1)
    scope: str = "plan"


class ExecutionOutcome(StrictModel):
    plan_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    execution_attempt_id: str = Field(min_length=1)
    adapter_request_id: str | None = None
    status: ExecutionStatus
    adapter_ref: SourceRef | None = None
    before_state: StateSnapshot | None = None
    after_state: StateSnapshot | None = None
    error: ErrorDetail | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionSummary(StrictModel):
    outcomes: list[ExecutionOutcome] = Field(default_factory=list)


class Plan(StrictModel):
    id: str = Field(min_length=1)
    schema_version: str = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    execute_at: datetime | None = None
    commands: list[Command] = Field(min_length=1, max_length=50)
    status: PlanStatus = PlanStatus.DRAFT
    validation: ValidationResult | None = None
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    approval: Approval | None = None
    execution: ExecutionSummary | None = None
    agent_request_id: str | None = None

    @model_validator(mode="after")
    def validate_timestamps(self) -> Plan:
        for field_name, value in (
            ("created_at", self.created_at),
            ("expires_at", self.expires_at),
            ("execute_at", self.execute_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        return self


class RecurrenceRule(StrictModel):
    time_of_day: time
    timezone: str = Field(min_length=1)
    days_of_week: list[int] | None = None

    @model_validator(mode="after")
    def validate_days_of_week(self) -> RecurrenceRule:
        if self.days_of_week is not None:
            if not self.days_of_week:
                raise ValueError("days_of_week, if provided, must not be empty")
            if any(day < 0 or day > 6 for day in self.days_of_week):
                raise ValueError("days_of_week entries must be in 0..6 (0=Monday)")
        return self


class AuditEvent(StrictModel):
    id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AdapterSnapshot(StrictModel):
    source_entities: list[dict[str, Any]] = Field(default_factory=list)
    source_states: list[dict[str, Any]] = Field(default_factory=list)
    areas: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_sources: list[dict[str, Any]] = Field(default_factory=list)


class AdapterHealth(StrictModel):
    adapter_id: str = Field(min_length=1)
    connected: bool
    message: str | None = None
    components: list[AdapterHealth] | None = None


class AdapterExecutionAck(StrictModel):
    accepted: bool
    source_ref: SourceRef | None = None
    message: str | None = None


class StateChangedEvent(StrictModel):
    kind: Literal["state_changed"] = "state_changed"
    payload: dict[str, Any] = Field(default_factory=dict)


class AvailabilityChangedEvent(StrictModel):
    kind: Literal["availability_changed"] = "availability_changed"
    payload: dict[str, Any] = Field(default_factory=dict)


class DeviceMembershipChangedEvent(StrictModel):
    kind: Literal["device_membership_changed"] = "device_membership_changed"
    payload: dict[str, Any] = Field(default_factory=dict)


class MetadataChangedEvent(StrictModel):
    kind: Literal["metadata_changed"] = "metadata_changed"
    payload: dict[str, Any] = Field(default_factory=dict)


class AdapterDiagnosticEvent(StrictModel):
    kind: Literal["adapter_diagnostic"] = "adapter_diagnostic"
    payload: dict[str, Any] = Field(default_factory=dict)


type SourceEvent = (
    StateChangedEvent
    | AvailabilityChangedEvent
    | DeviceMembershipChangedEvent
    | MetadataChangedEvent
    | AdapterDiagnosticEvent
)
