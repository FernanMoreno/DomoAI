"""Canonical, transport-independent DomoAI models."""

from __future__ import annotations

import hashlib
import json
import math
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


class ControlLeaseStatus(StrEnum):
    ACQUIRED = "acquired"
    REJECTED = "rejected"
    EXPIRED = "expired"
    RELEASED = "released"


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


class DependencyKind(StrEnum):
    ASSUMPTION = "assumption"
    PRECONDITION = "precondition"
    PREDECESSOR_OUTCOME = "predecessor_outcome"


class ExecutionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONFIRMED_SUCCESS = "confirmed_success"
    UNKNOWN = "unknown"


class BundleCommitStatus(StrEnum):
    COMMITTING = "committing"
    COMPLETED = "completed"
    SCHEDULED = "scheduled"
    PARTIALLY_COMMITTED = "partially_committed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    MISSED = "missed"


class BundleMemberCommitStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    SCHEDULED = "scheduled"
    FAILED = "failed"
    UNKNOWN = "unknown"
    MISSED = "missed"
    # Never dispatched: a required predecessor did not reach
    # confirmed_success. Distinct from FAILED (adapter was called and the
    # outcome was not success) so operators/replan logic can tell "we tried
    # and it failed" from "we correctly refused to try".
    DEPENDENCY_FAILED = "dependency_failed"


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
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must be less than or equal to maximum")
        if self.kind not in {CapabilityKind.INTEGER, CapabilityKind.NUMBER}:
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("numeric bounds are only valid for integer or number capabilities")
        if self.kind is CapabilityKind.ENUM and not self.enum_values:
            raise ValueError("enum capabilities require enum_values")
        step = self.constraints.get("step")
        if step is not None:
            if self.kind not in {CapabilityKind.INTEGER, CapabilityKind.NUMBER}:
                raise ValueError("step is only valid for numeric capabilities")
            if (
                not isinstance(step, (int, float))
                or isinstance(step, bool)
                or not math.isfinite(float(step))
                or float(step) <= 0
            ):
                raise ValueError("step must be a finite positive number")
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


class ExecutionWindow(StrictModel):
    """Immutable temporal evidence attached to a scheduled physical intent."""

    intended_at: datetime
    not_before: datetime
    not_after: datetime
    timezone: str = Field(min_length=1)
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_window(self) -> ExecutionWindow:
        values = {
            "intended_at": self.intended_at,
            "not_before": self.not_before,
            "not_after": self.not_after,
        }
        if any(value.tzinfo is None or value.utcoffset() is None for value in values.values()):
            raise ValueError("execution window instants must be timezone-aware")
        if self.not_before > self.intended_at or self.intended_at > self.not_after:
            raise ValueError("execution window must satisfy not_before <= intended_at <= not_after")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        def canonical(value: datetime) -> str:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

        return {
            "intended_at": canonical(self.intended_at),
            "not_before": canonical(self.not_before),
            "not_after": canonical(self.not_after),
            "timezone": self.timezone,
            "revision": self.revision,
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class Precondition(StrictModel):
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    expected: ScalarValue | None
    allow_stale: bool = False


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


class CommandPostcondition(StrictModel):
    """Typed observation required before a command is confirmed successful."""

    capability: str = Field(min_length=1)
    expected: ScalarValue | None = None
    verification: Literal["equals", "toggle_transition", "motion_stopped", "unconfirmed"] = (
        "equals"
    )
    tolerance: float | None = Field(default=None, ge=0)
    settle_timeout_seconds: float | None = Field(default=None, ge=0, le=120)
    poll_interval_seconds: float = Field(default=0.25, gt=0, le=10)
    reconcile_capabilities: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_tolerance(self) -> CommandPostcondition:
        if self.verification == "equals" and self.expected is None:
            raise ValueError("equals verification requires an expected value")
        if self.verification == "toggle_transition" and self.expected is not None:
            raise ValueError("toggle_transition verification derives its expected value")
        if self.verification == "unconfirmed" and self.expected is not None:
            raise ValueError("unconfirmed verification must not declare an expected value")
        if self.tolerance is None:
            return self
        numeric_expected = isinstance(self.expected, (int, float)) and not isinstance(
            self.expected, bool
        )
        if not numeric_expected:
            raise ValueError("tolerance is only valid for numeric expected values")
        expected_value = self.expected
        assert isinstance(expected_value, (int, float)) and not isinstance(expected_value, bool)
        if not math.isfinite(float(expected_value)) or not math.isfinite(self.tolerance):
            raise ValueError("postcondition numeric values must be finite")
        return self

    @model_validator(mode="after")
    def validate_settling(self) -> CommandPostcondition:
        if not math.isfinite(self.poll_interval_seconds):
            raise ValueError("poll interval must be finite")
        if self.settle_timeout_seconds is not None:
            if not math.isfinite(self.settle_timeout_seconds):
                raise ValueError("settle timeout must be finite")
            if (
                self.settle_timeout_seconds > 0
                and self.poll_interval_seconds > self.settle_timeout_seconds
            ):
                raise ValueError("poll interval must not exceed settle timeout")
        return self

    @model_validator(mode="after")
    def validate_reconciliation(self) -> CommandPostcondition:
        if any(not capability.strip() for capability in self.reconcile_capabilities):
            raise ValueError("reconcile capabilities must be non-empty")
        if len(set(self.reconcile_capabilities)) != len(self.reconcile_capabilities):
            raise ValueError("reconcile capabilities must be unique")
        return self


class Command(StrictModel):
    id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    value: ScalarValue | None = None
    unit: str | None = None
    preconditions: list[Precondition] = Field(default_factory=list, max_length=16)
    risk_class: RiskClass = RiskClass.SAFE
    idempotency_key: str = Field(min_length=1)
    intent: str | None = None
    postconditions: list[CommandPostcondition] = Field(default_factory=list, max_length=1)


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
    dependency_kinds: dict[str, DependencyKind] = Field(default_factory=dict)
    capability_fingerprints: dict[str, str] = Field(default_factory=dict)


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
    allows_stale: bool = False


class PolicyDecision(StrictModel):
    policy_id: str | None = None
    action: PolicyAction
    reason: str = Field(min_length=1)
    allows_stale: bool = False


class Approval(StrictModel):
    status: str = Field(pattern=r"^(approved|denied|expired)$")
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    validation_digest: str = Field(min_length=1)
    scope: str = "plan"
    authentication_context: str | None = None
    session_id: str | None = None
    window_digest: str | None = None
    schedule_revision: int = Field(default=0, ge=0)


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


class ControlLease(StrictModel):
    """Evidence-backed ownership lease for a physically controlled device."""

    lease_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    status: ControlLeaseStatus
    acquired_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> ControlLease:
        if self.acquired_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("control lease timestamps must be timezone-aware")
        if self.expires_at <= self.acquired_at:
            raise ValueError("control lease must expire after acquisition")
        return self


class PhysicalBaseline(StrictModel):
    """Physical observation captured before a control lease is used."""

    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    power_kw: float | None = None
    observed_at: datetime
    received_at: datetime
    source_ref: SourceRef
    state_revision: str = Field(min_length=1)
    native_scheduler_status: Literal["disabled", "inactive", "active", "unknown"]

    @model_validator(mode="after")
    def validate_observation(self) -> PhysicalBaseline:
        if self.observed_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("physical baseline timestamps must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("baseline received_at must not precede observed_at")
        if self.power_kw is not None and not math.isfinite(self.power_kw):
            raise ValueError("baseline power must be finite")
        return self


class TakeoverResult(StrictModel):
    """Result and evidence of the pre-write control acquisition handshake."""

    lease_id: str = Field(min_length=1)
    status: ControlLeaseStatus
    owner: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    acquired_at: datetime
    expires_at: datetime
    baseline: PhysicalBaseline | None = None
    first_command_id: str = Field(min_length=1)
    first_command_confirmed: bool = False
    confirmed_at: datetime | None = None
    failure_code: str | None = None
    evidence_digest: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> TakeoverResult:
        if self.acquired_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("takeover timestamps must be timezone-aware")
        if self.expires_at <= self.acquired_at:
            raise ValueError("takeover must expire after acquisition")
        if self.confirmed_at is not None and self.confirmed_at.tzinfo is None:
            raise ValueError("confirmed_at must be timezone-aware")
        if self.status is ControlLeaseStatus.ACQUIRED and self.baseline is None:
            raise ValueError("acquired takeover requires a physical baseline")
        if self.status is ControlLeaseStatus.REJECTED and not self.failure_code:
            raise ValueError("rejected takeover requires failure_code")
        return self


class ExecutionDependencyEvidence(StrictModel):
    predecessor_plan_id: str = Field(min_length=1)
    predecessor_command_ids: list[str] = Field(min_length=1)
    status: ExecutionStatus
    state_versions: dict[str, int] = Field(default_factory=dict)
    captured_at: datetime
    evidence_digest: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_capture_time(self) -> ExecutionDependencyEvidence:
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if self.status is not ExecutionStatus.CONFIRMED_SUCCESS:
            raise ValueError("only confirmed success can publish dependency evidence")
        return self


class BundleMemberCommit(StrictModel):
    plan_id: str = Field(min_length=1)
    validation_digest: str = Field(min_length=1)
    execute_at: datetime | None = None
    status: BundleMemberCommitStatus = BundleMemberCommitStatus.PENDING
    execution_status: ExecutionStatus | None = None
    scheduled: bool = False
    error_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    predecessor_plan_id: str | None = None
    predecessor_outcome_digest: str | None = None

    @model_validator(mode="after")
    def validate_execute_at(self) -> BundleMemberCommit:
        if self.execute_at is not None and self.execute_at.tzinfo is None:
            raise ValueError("execute_at must be timezone-aware")
        if self.predecessor_outcome_digest is not None and self.predecessor_plan_id is None:
            raise ValueError("predecessor outcome digest requires predecessor plan")
        return self


class BundleCommit(StrictModel):
    id: str = Field(min_length=1)
    schema_version: str = SCHEMA_VERSION
    bundle_digest: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    status: BundleCommitStatus = BundleCommitStatus.COMMITTING
    compensation_policy: Literal["none"] = "none"
    members: list[BundleMemberCommit] = Field(min_length=1, max_length=50)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    failure: ErrorDetail | None = None

    @model_validator(mode="after")
    def validate_timestamps(self) -> BundleCommit:
        for field_name, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        return self


class Plan(StrictModel):
    id: str = Field(min_length=1)
    schema_version: str = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    execute_at: datetime | None = None
    execution_window: ExecutionWindow | None = None
    schedule_revision: int = Field(default=0, ge=0)
    commands: list[Command] = Field(min_length=1, max_length=50)
    status: PlanStatus = PlanStatus.DRAFT
    definition_digest: str | None = None
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
        if self.execution_window is not None:
            if self.execute_at is None:
                raise ValueError("execution_window requires execute_at")
            if self.execute_at != self.execution_window.intended_at:
                raise ValueError("execution_window intended_at must match execute_at")
            if self.schedule_revision != self.execution_window.revision:
                raise ValueError("schedule_revision must match execution_window revision")
        return self


class RecurrenceRule(StrictModel):
    time_of_day: time
    timezone: str = Field(min_length=1)
    days_of_week: list[int] | None = None
    # Optional bound on how long this automation stays active. None means
    # unbounded (unchanged default behavior); callers that want a
    # self-expiring automation (e.g. a seasonal schedule) set it explicitly.
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_days_of_week(self) -> RecurrenceRule:
        if self.days_of_week is not None:
            if not self.days_of_week:
                raise ValueError("days_of_week, if provided, must not be empty")
            if any(day < 0 or day > 6 for day in self.days_of_week):
                raise ValueError("days_of_week entries must be in 0..6 (0=Monday)")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
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
    source_adapter_id: str | None = None
    occurred_at: datetime | None = None
    external_id: str | None = None
    device_id: str | None = None
    capability: str | None = None
    value: Any = None
    unit: str | None = None
    available: bool | None = None
    capabilities: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def decode_legacy_payload(cls, value: Any) -> Any:
        return _decode_event_payload(value, {
            "source_adapter_id": ("source_adapter_id",),
            "external_id": ("external_id", "entity_id", "friendly_name", "node_id"),
            "capability": ("capability",),
            "value": ("value",),
            "unit": ("unit",),
            "available": ("available",),
            "capabilities": ("capabilities",),
            "occurred_at": ("occurred_at", "received_at", "observed_at"),
        })


class AvailabilityChangedEvent(StrictModel):
    kind: Literal["availability_changed"] = "availability_changed"
    source_adapter_id: str | None = None
    occurred_at: datetime | None = None
    external_id: str | None = None
    available: bool | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def decode_legacy_payload(cls, value: Any) -> Any:
        return _decode_event_payload(value, {
            "source_adapter_id": ("source_adapter_id",),
            "external_id": ("external_id", "entity_id", "friendly_name", "node_id"),
            "available": ("available",),
            "occurred_at": ("occurred_at", "received_at", "observed_at"),
        })


class DeviceMembershipChangedEvent(StrictModel):
    kind: Literal["device_membership_changed"] = "device_membership_changed"
    source_adapter_id: str | None = None
    occurred_at: datetime | None = None
    external_id: str | None = None
    friendly_name: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def decode_legacy_payload(cls, value: Any) -> Any:
        return _decode_event_payload(value, {
            "source_adapter_id": ("source_adapter_id",),
            "external_id": ("external_id", "entity_id", "friendly_name", "node_id"),
            "friendly_name": ("friendly_name",),
            "capabilities": ("capabilities",),
            "occurred_at": ("occurred_at", "received_at", "observed_at"),
        })


class MetadataChangedEvent(StrictModel):
    kind: Literal["metadata_changed"] = "metadata_changed"
    source_adapter_id: str | None = None
    occurred_at: datetime | None = None
    external_id: str | None = None
    friendly_name: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def decode_legacy_payload(cls, value: Any) -> Any:
        return _decode_event_payload(value, {
            "source_adapter_id": ("source_adapter_id",),
            "external_id": ("external_id", "entity_id", "friendly_name", "node_id"),
            "friendly_name": ("friendly_name",),
            "capabilities": ("capabilities",),
            "occurred_at": ("occurred_at", "received_at", "observed_at"),
        })


class AdapterDiagnosticEvent(StrictModel):
    kind: Literal["adapter_diagnostic"] = "adapter_diagnostic"
    source_adapter_id: str | None = None
    occurred_at: datetime | None = None
    external_id: str | None = None
    code: str | None = None
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def decode_legacy_payload(cls, value: Any) -> Any:
        return _decode_event_payload(value, {
            "source_adapter_id": ("source_adapter_id",),
            "external_id": ("external_id", "entity_id", "friendly_name", "node_id"),
            "code": ("code", "diagnostic_code"),
            "message": ("message", "reason"),
            "occurred_at": ("occurred_at", "received_at", "observed_at"),
        })


type SourceEvent = (
    StateChangedEvent
    | AvailabilityChangedEvent
    | DeviceMembershipChangedEvent
    | MetadataChangedEvent
    | AdapterDiagnosticEvent
)


def _decode_event_payload(value: Any, fields: dict[str, tuple[str, ...]]) -> Any:
    """Populate typed metadata while accepting legacy adapter payloads."""

    if not isinstance(value, dict):
        return value
    result = dict(value)
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return result
    for field_name, aliases in fields.items():
        if result.get(field_name) is not None:
            continue
        for alias in aliases:
            if payload.get(alias) is not None:
                raw_value = payload[alias]
                result[field_name] = (
                    str(raw_value)
                    if field_name in {"external_id", "device_id"}
                    else raw_value
                )
                break
    return result
