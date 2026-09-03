"""Versioned, non-authoritative evidence for future hardware commissioning."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import (
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


__all__ = [
    "CommissioningAssetType",
    "CommissioningBlocker",
    "CommissioningCandidate",
    "CommissioningCandidateStatus",
    "CommissioningReport",
    "CommissioningRoute",
]
