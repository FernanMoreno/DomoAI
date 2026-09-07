"""Versioned, non-authoritative evidence for future hardware commissioning."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import (
    AuthorityContext,
    DeviceType,
    SourceRef,
    StrictModel,
)

_SHA256 = r"^[0-9a-f]{64}$"


class CommissioningAssetType(StrEnum):
    BATTERY = "battery"
    EV_CHARGER = "ev_charger"


class CommissioningCandidateStatus(StrEnum):
    READY_FOR_BINDING = "ready_for_binding"
    OBSERVED_ONLY = "observed_only"
    BLOCKED = "blocked"


class CommissioningEvidenceClass(StrEnum):
    SIMULATION = "simulation"
    HARDWARE = "hardware"
    EXTERNAL_DEPENDENCY = "external_dependency"


class CommissioningCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class CommissioningQualificationStatus(StrEnum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    BLOCKED_EXTERNAL_DEPENDENCY = "blocked_external_dependency"


class CommissioningCheck(StrictModel):
    check_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    status: CommissioningCheckStatus
    detail: str = Field(min_length=1, max_length=256)


class CommissioningBlocker(StrictModel):
    """A stable reason why a candidate needs an explicit operator decision."""

    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    capability: str | None = Field(default=None, min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=256)


class CommissioningRoute(StrictModel):
    """Sanitized projection of a source route; never an execution grant."""

    schema_version: Literal["v1"] = "v1"
    provider_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    capability: str = Field(min_length=1, max_length=128)
    source_ref: SourceRef
    source_device_id: str = Field(min_length=1, max_length=256)
    commands: list[str] = Field(default_factory=list, max_length=64)
    readable: bool
    writable: bool
    available: bool

    @model_validator(mode="after")
    def validate_source(self) -> CommissioningRoute:
        if self.source_ref.adapter_id != self.provider_id:
            raise ValueError("commissioning route provider must match source_ref.adapter_id")
        if self.source_ref.source_device_id is not None and (
            self.source_ref.source_device_id != self.source_device_id
        ):
            raise ValueError("commissioning route source device identity must match source_ref")
        if not self.writable and self.commands:
            raise ValueError("read-only commissioning routes cannot expose commands")
        return self


class CommissioningCandidate(StrictModel):
    """A discovered asset assessment, deliberately weaker than a binding."""

    schema_version: Literal["v1"] = "v1"
    asset_type: CommissioningAssetType
    canonical_device_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    device_type: DeviceType
    provider_ids: list[str] = Field(min_length=1, max_length=32)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=64)
    identity_keys: list[str] = Field(default_factory=list, max_length=64)
    connections: list[str] = Field(default_factory=list, max_length=64)
    required_capabilities: list[str] = Field(min_length=1, max_length=16)
    routes: list[CommissioningRoute] = Field(default_factory=list, max_length=128)
    status: CommissioningCandidateStatus
    blockers: list[CommissioningBlocker] = Field(default_factory=list, max_length=32)
    next_actions: list[str] = Field(default_factory=list, max_length=16)
    candidate_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_identity_and_status(self) -> CommissioningCandidate:
        if len(set(self.provider_ids)) != len(self.provider_ids):
            raise ValueError("commissioning provider_ids must be unique")
        source_keys = {(ref.adapter_id, ref.external_id) for ref in self.source_refs}
        if len(source_keys) != len(self.source_refs):
            raise ValueError("commissioning source_refs must be unique")
        if self.status is CommissioningCandidateStatus.READY_FOR_BINDING and self.blockers:
            raise ValueError("ready_for_binding candidates cannot contain blockers")
        if self.status is CommissioningCandidateStatus.BLOCKED and not self.blockers:
            raise ValueError("blocked candidates require blockers")
        return self


class CommissioningReport(StrictModel):
    """Runtime-wide commissioning evidence shared by every MCP client."""

    schema_version: Literal["v1"] = "v1"
    authority: AuthorityContext = Field(default_factory=AuthorityContext)
    runtime_revision: str = Field(min_length=1, max_length=128)
    generated_at: datetime
    report_digest: str = Field(pattern=_SHA256)
    candidates: list[CommissioningCandidate] = Field(default_factory=list, max_length=128)
    warnings: list[str] = Field(default_factory=list, max_length=64)
    # A literal false prevents a future caller from accidentally treating the
    # report as a mutation response or authority artifact.
    authority_created: Literal[False] = False

    @model_validator(mode="after")
    def validate_timestamp(self) -> CommissioningReport:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("commissioning report timestamp must be timezone-aware")
        return self


class CommissioningEvidence(StrictModel):
    """Bounded operator evidence; it is not an execution grant."""

    schema_version: Literal["v1"] = "v1"
    authority: AuthorityContext
    evidence_id: str = Field(min_length=1, max_length=128)
    candidate_digest: str = Field(pattern=_SHA256)
    observed_at: datetime
    expires_at: datetime
    evidence_class: CommissioningEvidenceClass
    checks: list[CommissioningCheck] = Field(min_length=1, max_length=32)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=64)
    evidence_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> CommissioningEvidence:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("commissioning evidence observed_at must be timezone-aware")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("commissioning evidence expires_at must be timezone-aware")
        if self.expires_at <= self.observed_at:
            raise ValueError("commissioning evidence must expire after observation")
        check_ids = [check.check_id for check in self.checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("commissioning evidence check ids must be unique")
        expected = commissioning_evidence_digest(self)
        if self.evidence_digest is None:
            object.__setattr__(self, "evidence_digest", expected)
        elif self.evidence_digest != expected:
            raise ValueError("commissioning evidence digest does not match evidence")
        return self


class CommissioningQualification(StrictModel):
    """Verification result that cannot create physical authority."""

    schema_version: Literal["v1"] = "v1"
    authority: AuthorityContext
    candidate_digest: str = Field(pattern=_SHA256)
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: CommissioningQualificationStatus
    verified_checks: list[str] = Field(default_factory=list, max_length=32)
    blockers: list[str] = Field(default_factory=list, max_length=32)
    checked_at: datetime
    authority_created: Literal[False] = False

    @model_validator(mode="after")
    def validate_result(self) -> CommissioningQualification:
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("commissioning qualification checked_at must be timezone-aware")
        if self.status is CommissioningQualificationStatus.QUALIFIED and self.blockers:
            raise ValueError("qualified commissioning result cannot contain blockers")
        if self.status is not CommissioningQualificationStatus.QUALIFIED and not self.blockers:
            raise ValueError("non-qualified commissioning result requires blockers")
        return self


def commissioning_evidence_digest(evidence: CommissioningEvidence) -> str:
    """Return a stable digest excluding the digest field itself."""

    payload = evidence.model_dump(mode="json", exclude={"evidence_digest"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


__all__ = [
    "CommissioningCheck",
    "CommissioningCheckStatus",
    "CommissioningAssetType",
    "CommissioningBlocker",
    "CommissioningCandidate",
    "CommissioningCandidateStatus",
    "CommissioningReport",
    "CommissioningRoute",
    "CommissioningEvidence",
    "CommissioningEvidenceClass",
    "CommissioningQualification",
    "CommissioningQualificationStatus",
    "commissioning_evidence_digest",
]
