"""Canonical, sanitized evidence for attended multi-host qualification."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from domoai.domain.coordination import LeaseScope
from domoai.domain.models import StrictModel

_DIGEST = r"^sha256:[0-9a-f]{64}$"
_SENSITIVE_KEY = re.compile(
    r"(?:token|password|passwd|secret|bearer|credential|authorization|api[_-]?key)",
    re.IGNORECASE,
)

REQUIRED_MULTIHOST_CHECKS = frozenset(
    {
        "etcd_quorum",
        "etcd_lease_renewal",
        "etcd_takeover",
        "postgres_primary",
        "postgres_sync_replica",
        "gateway_current_epoch",
        "gateway_stale_epoch",
        "gateway_replay_epoch",
    }
)


class MultiHostQualificationCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class MultiHostQualificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class MultiHostQualificationError(ValueError):
    """Raised when production multi-host evidence cannot be trusted."""


def _assert_safe_details(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                raise ValueError("qualification details contain sensitive material")
            _assert_safe_details(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_safe_details(item)


class MultiHostQualificationCheck(StrictModel):
    """One bounded observation from an external multi-host qualification run."""

    check_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    status: MultiHostQualificationCheckStatus
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_details(self) -> MultiHostQualificationCheck:
        _assert_safe_details(self.details)
        return self


class MultiHostQualificationEvidence(StrictModel):
    """A scope-bound proof that active-passive multi-host gates passed."""

    schema_version: Literal["v1"] = "v1"
    qualification_environment: Literal["production", "lab"] = "production"
    scope: LeaseScope
    gateway_identity: str = Field(min_length=1, max_length=200)
    completed_at: datetime
    expires_at: datetime
    checks: list[MultiHostQualificationCheck] = Field(min_length=1, max_length=32)
    status: MultiHostQualificationStatus = MultiHostQualificationStatus.BLOCKED
    evidence_digest: str | None = Field(default=None, pattern=_DIGEST)

    @field_validator("completed_at", "expires_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("qualification timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def derive_status_and_digest(self) -> MultiHostQualificationEvidence:
        if self.expires_at <= self.completed_at:
            raise ValueError("qualification expiry must follow completion")
        check_ids = [check.check_id for check in self.checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("qualification checks must be unique")
        if set(check_ids) != REQUIRED_MULTIHOST_CHECKS:
            raise ValueError("qualification evidence must include all required checks")
        if any(check.status is MultiHostQualificationCheckStatus.FAILED for check in self.checks):
            self.status = MultiHostQualificationStatus.FAILED
        elif any(
            check.status is MultiHostQualificationCheckStatus.BLOCKED for check in self.checks
        ):
            self.status = MultiHostQualificationStatus.BLOCKED
        else:
            self.status = MultiHostQualificationStatus.PASSED
        expected = multihost_qualification_digest(self)
        if self.evidence_digest is None:
            self.evidence_digest = expected
        elif self.evidence_digest != expected:
            if (
                "qualification_environment" not in self.model_fields_set
                and self.evidence_digest == _legacy_multihost_qualification_digest(self)
            ):
                self.evidence_digest = expected
            else:
                raise ValueError("qualification evidence digest does not match contents")
        return self

    def qualifies(
        self,
        scope: LeaseScope,
        *,
        gateway_identity: str,
        now: datetime,
    ) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("qualification comparison time must be timezone-aware")
        return (
            self.status is MultiHostQualificationStatus.PASSED
            and self.qualification_environment == "production"
            and self.scope == scope
            and self.gateway_identity == gateway_identity
            and self.expires_at > now.astimezone(UTC)
            and self.evidence_digest == multihost_qualification_digest(self)
        )


class GatewayFencingProbeRequest(StrictModel):
    """One explicitly safe physical-boundary fencing probe."""

    schema_version: Literal["v1"] = "v1"
    probe_id: UUID
    scope: LeaseScope
    fencing_epoch: int = Field(gt=0)
    lease_id: str = Field(min_length=1, max_length=200)
    safe_command: str = Field(min_length=1, max_length=200)


class GatewayFencingProbeResult(StrictModel):
    """The non-secret decision made by the final physical gateway."""

    schema_version: Literal["v1"] = "v1"
    probe_id: UUID
    accepted: bool
    observed_epoch: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=200)


def multihost_qualification_digest(evidence: MultiHostQualificationEvidence) -> str:
    """Return the stable SHA-256 digest of evidence excluding its digest field."""

    payload = evidence.model_dump(mode="json", exclude={"evidence_digest"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _legacy_multihost_qualification_digest(evidence: MultiHostQualificationEvidence) -> str:
    """Return the pre-provenance digest for a field-absent v1 artifact."""

    payload = evidence.model_dump(
        mode="json", exclude={"evidence_digest", "qualification_environment"}
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def load_multihost_qualification_evidence(path: Path) -> MultiHostQualificationEvidence:
    """Load a regular-file v1 qualification artifact without following links."""

    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("qualification evidence must be a regular file")
        return MultiHostQualificationEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise MultiHostQualificationError(
            "multi-host qualification evidence is unavailable or not valid v1 JSON"
        ) from error


__all__ = [
    "REQUIRED_MULTIHOST_CHECKS",
    "MultiHostQualificationCheck",
    "MultiHostQualificationCheckStatus",
    "MultiHostQualificationEvidence",
    "MultiHostQualificationError",
    "MultiHostQualificationStatus",
    "GatewayFencingProbeRequest",
    "GatewayFencingProbeResult",
    "multihost_qualification_digest",
    "load_multihost_qualification_evidence",
]
