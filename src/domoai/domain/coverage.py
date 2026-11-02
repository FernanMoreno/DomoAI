"""Read-only coverage contracts; v1 deliberately makes no hardware claims."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import CapabilityGuarantees, DeviceType, StrictModel

type EvidenceReason = Literal[
    "evidence_not_bound",
    "protocol_unverified",
    "profile_unverified",
    "firmware_unverified",
    "identity_unverified",
    "tested_at_unverified",
    "expiry_unverified",
]
type CoverageFamilyName = Literal[
    "lighting",
    "metered_plugs",
    "covers",
    "hvac",
    "schedulable_loads",
]
type AvailabilityReason = Literal["source_unavailable", "provider_inactive"]
type MissingRequirement = Literal[
    "hardware_evidence_missing",
    "not_present_in_visible_inventory",
    "requested_functions_unspecified",
    "plug_identity_unverified",
    "metering_missing",
    "position_feedback_missing",
    "control_missing",
    "temperature_feedback_missing",
    "setpoint_control_missing",
    "programmability_unverified",
    "resume_behavior_unverified",
]


class UnknownCoverageEvidence(StrictModel):
    level: Literal["unknown"] = "unknown"
    status: Literal["not_verified"] = "not_verified"
    protocol: None = None
    profile: None = None
    firmware: None = None
    hardware_identity: None = None
    tested_at: None = None
    expires_at: None = None
    verified_capabilities: list[str] = Field(default_factory=list, max_length=0)
    verified_operations: list[str] = Field(default_factory=list, max_length=0)
    reasons: list[EvidenceReason] = Field(
        default_factory=lambda: list[EvidenceReason](
            [
                "evidence_not_bound",
                "protocol_unverified",
                "profile_unverified",
                "firmware_unverified",
                "identity_unverified",
                "tested_at_unverified",
                "expiry_unverified",
            ]
        ),
        min_length=1,
        max_length=7,
    )


class CoverageOperations(StrictModel):
    read: bool
    write: bool
    commands: list[str]


class CoverageLimits(StrictModel):
    unit: str | None
    minimum: float | int | None
    maximum: float | int | None
    enum_values: list[str]


class CoverageEvidenceRoute(StrictModel):
    route_key: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provider_id: str = Field(min_length=1)
    source_available: bool
    provider_active: bool
    effective_available: bool
    availability_reasons: list[AvailabilityReason]
    declared_operations: CoverageOperations
    evidence: UnknownCoverageEvidence = Field(default_factory=UnknownCoverageEvidence)


class CoverageRowRef(StrictModel):
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)


class CoverageEvidenceRow(CoverageRowRef):
    device_type: DeviceType
    canonical_protocol_label: str
    declared_operations: CoverageOperations
    declared_guarantees: CapabilityGuarantees
    limits: CoverageLimits
    status: Literal["active", "unavailable"]
    reasons: list[Literal["route_missing"]]
    evidence_level: Literal["unknown"] = "unknown"
    evidence_status: Literal["not_verified"] = "not_verified"
    physical_identity_status: Literal["unverified"] = "unverified"
    routes: list[CoverageEvidenceRoute]


class CoverageFamily(StrictModel):
    family: CoverageFamilyName
    priority: int = Field(ge=1, le=5)
    candidate_device_ids: list[str]
    row_refs: list[CoverageRowRef]
    status: Literal["not_qualified"] = "not_qualified"
    missing_requirements: list[MissingRequirement] = Field(min_length=1)
    hardware_verified: Literal[False] = False


class CoverageEvidenceSnapshot(StrictModel):
    schema_version: Literal["v1"] = "v1"
    runtime_revision: str
    rows: list[CoverageEvidenceRow]
    families: list[CoverageFamily]

    @model_validator(mode="after")
    def validate_references(self) -> CoverageEvidenceSnapshot:
        row_keys = {(row.device_id, row.capability) for row in self.rows}
        if len(row_keys) != len(self.rows):
            raise ValueError("coverage rows must be unique per canonical device and capability")
        for row in self.rows:
            if len({route.route_key for route in row.routes}) != len(row.routes):
                raise ValueError("coverage route keys must be unique within a row")
        for group in self.families:
            if any(
                (ref.device_id, ref.capability) not in row_keys
                or ref.device_id not in group.candidate_device_ids
                for ref in group.row_refs
            ):
                raise ValueError("coverage family must reference its visible candidate rows")
        return self
