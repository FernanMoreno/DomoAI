"""Versioned, sanitized evidence contracts for digital-twin qualification."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from domoai.domain.models import StrictModel

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:token|password|passwd|secret|bearer|credential|authorization|api[_-]?key)",
    re.IGNORECASE,
)


class DigitalTwinScope(StrEnum):
    DIGITAL_TWIN = "digital_twin"


class DigitalTwinRunStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class DigitalTwinCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


def _validate_unique_names(values: list[str], field_name: str) -> list[str]:
    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} must not contain blank values")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must contain unique values")
    return values


def _assert_safe_details(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                raise ValueError("digital-twin evidence details contain sensitive material")
            _assert_safe_details(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_safe_details(item)


class DigitalTwinCoverage(StrictModel):
    """The adapter, domain and cross-cutting surfaces exercised by a run."""

    adapters: list[str] = Field(min_length=1)
    domains: list[str] = Field(min_length=1)
    checks: list[str] = Field(min_length=1)

    _unique_adapters = field_validator("adapters")(
        lambda values: _validate_unique_names(values, "adapters")
    )
    _unique_domains = field_validator("domains")(
        lambda values: _validate_unique_names(values, "domains")
    )
    _unique_checks = field_validator("checks")(
        lambda values: _validate_unique_names(values, "checks")
    )


class DigitalTwinCheck(StrictModel):
    """One deterministic check result with no raw protocol credentials."""

    check_id: str = Field(min_length=1, max_length=300)
    status: DigitalTwinCheckStatus = DigitalTwinCheckStatus.PASSED
    code: str | None = Field(default=None, min_length=1, max_length=100)
    message: str | None = Field(default=None, max_length=500)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("check_id")
    @classmethod
    def validate_check_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("check_id must not be blank")
        return value

    @model_validator(mode="after")
    def validate_safe_details(self) -> DigitalTwinCheck:
        _assert_safe_details(self.details)
        if self.status is DigitalTwinCheckStatus.FAILED and not self.code:
            raise ValueError("failed digital-twin checks require a diagnostic code")
        return self


class DigitalTwinEvidence(StrictModel):
    """Machine-readable proof of the software closed loop on a virtual plant."""

    schema_version: Literal["v1"] = "v1"
    run_id: str = Field(min_length=1, max_length=200)
    scope: Literal["digital_twin"] = DigitalTwinScope.DIGITAL_TWIN.value
    status: DigitalTwinRunStatus = DigitalTwinRunStatus.PASSED
    seed: int = Field(gt=0)
    plant_digest: str = Field(pattern=_DIGEST.pattern)
    trace_digest: str = Field(pattern=_DIGEST.pattern)
    coverage: DigitalTwinCoverage
    checks: list[DigitalTwinCheck] = Field(default_factory=list)
    invariant_violations: list[str] = Field(default_factory=list)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("run_id must not be blank")
        return value

    @field_validator("invariant_violations")
    @classmethod
    def validate_invariants(cls, values: list[str]) -> list[str]:
        return _validate_unique_names(values, "invariant_violations") if values else values

    @model_validator(mode="after")
    def derive_status_and_validate_scope(self) -> DigitalTwinEvidence:
        if any(check.status is DigitalTwinCheckStatus.FAILED for check in self.checks):
            self.status = DigitalTwinRunStatus.FAILED
        if self.invariant_violations:
            self.status = DigitalTwinRunStatus.FAILED
        return self

    def canonical_json(self) -> str:
        """Return stable JSON suitable for an evidence digest or replay."""

        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )

    def require_digital_twin_scope(self) -> DigitalTwinEvidence:
        """Guard consumers against treating a report as physical evidence."""

        if self.scope != DigitalTwinScope.DIGITAL_TWIN.value:
            raise ValueError("digital-twin evidence cannot be used as physical qualification")
        return self


__all__ = [
    "DigitalTwinCheck",
    "DigitalTwinCheckStatus",
    "DigitalTwinCoverage",
    "DigitalTwinEvidence",
    "DigitalTwinRunStatus",
    "DigitalTwinScope",
]
