"""Strict evidence contract for the Phase 4 local qualification preflight."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from domoai.domain.models import StrictModel

_SENSITIVE_KEY = re.compile(
    r"(?:token|password|passwd|secret|bearer|credential|authorization|api[_-]?key|dsn|private[_-]?key)",
    re.IGNORECASE,
)
SOFTWARE_GATE_IDS = frozenset({"digital_twin", "process_lab"})
PHYSICAL_GATE_IDS = frozenset(
    {"battery_hil", "live_protocol_commissioning", "external_provider"}
)


class Phase4GateKind(StrEnum):
    SOFTWARE = "software"
    PROCESS_LAB = "process_lab"
    PHYSICAL = "physical"


class Phase4GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked_external_dependency"


class Phase4PreflightStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED_EXTERNAL_DEPENDENCY = "blocked_external_dependency"


def _assert_secret_safe(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                raise ValueError("phase4 preflight details must be secret-safe")
            _assert_secret_safe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_secret_safe(item)


class Phase4Gate(StrictModel):
    """One observable software, process or external qualification gate."""

    gate_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    kind: Phase4GateKind
    status: Phase4GateStatus
    evidence_scope: Literal[
        "software", "process_lab", "external_blocked", "external_verified"
    ]
    details: dict[str, Any] = Field(default_factory=dict)
    exit_code: int | None = Field(default=None, ge=0)

    @field_validator("gate_id")
    @classmethod
    def validate_gate_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("gate_id must not be blank")
        return value

    @model_validator(mode="after")
    def validate_scope_and_details(self) -> Phase4Gate:
        _assert_secret_safe(self.details)
        expected_scope = {
            Phase4GateKind.SOFTWARE: "software",
            Phase4GateKind.PROCESS_LAB: "process_lab",
        }.get(self.kind)
        if expected_scope is not None and self.evidence_scope != expected_scope:
            raise ValueError("software gate evidence scope is invalid")
        if self.kind is Phase4GateKind.PHYSICAL:
            expected_physical_scope = (
                "external_blocked"
                if self.status is Phase4GateStatus.BLOCKED
                else "external_verified"
            )
            if self.evidence_scope != expected_physical_scope:
                raise ValueError("physical gate evidence scope is invalid")
        if self.status is Phase4GateStatus.PASSED and self.exit_code not in {None, 0}:
            raise ValueError("passed gates must have exit_code 0")
        if self.status is Phase4GateStatus.FAILED and self.exit_code == 0:
            raise ValueError("failed gates cannot have exit_code 0")
        return self


class Phase4PreflightReport(StrictModel):
    """Machine-readable, non-authoritative Phase 4 preflight evidence."""

    schema_version: Literal["v1"] = "v1"
    qualification_environment: Literal["local_preflight"] = "local_preflight"
    run_id: str = Field(min_length=1, max_length=200)
    seed: int = Field(gt=0)
    started_at: datetime
    completed_at: datetime
    status: Phase4PreflightStatus = Phase4PreflightStatus.BLOCKED_EXTERNAL_DEPENDENCY
    software_gates: list[Phase4Gate] = Field(min_length=1, max_length=8)
    physical_gates: list[Phase4Gate] = Field(min_length=1, max_length=8)
    residuals: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("preflight timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("residuals")
    @classmethod
    def validate_residuals(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("preflight residuals must not be blank")
        if len(set(values)) != len(values):
            raise ValueError("preflight residuals must be unique")
        return values

    @model_validator(mode="after")
    def derive_status_and_validate_gates(self) -> Phase4PreflightReport:
        if self.completed_at <= self.started_at:
            raise ValueError("preflight completed_at must follow started_at")
        software_ids = {gate.gate_id for gate in self.software_gates}
        physical_ids = {gate.gate_id for gate in self.physical_gates}
        if software_ids != SOFTWARE_GATE_IDS:
            raise ValueError("preflight software gates do not match the contract")
        if physical_ids != PHYSICAL_GATE_IDS:
            raise ValueError("preflight physical gates do not match the contract")
        expected_software_kinds = {
            "digital_twin": Phase4GateKind.SOFTWARE,
            "process_lab": Phase4GateKind.PROCESS_LAB,
        }
        if any(
            expected_software_kinds[gate.gate_id] is not gate.kind
            for gate in self.software_gates
        ):
            raise ValueError("software_gates contains an invalid gate kind")
        if any(gate.kind is not Phase4GateKind.PHYSICAL for gate in self.physical_gates):
            raise ValueError("physical_gates contains a non-physical gate")
        gates = [*self.software_gates, *self.physical_gates]
        if any(gate.status is Phase4GateStatus.FAILED for gate in gates):
            self.status = Phase4PreflightStatus.FAILED
        elif any(gate.status is Phase4GateStatus.BLOCKED for gate in gates):
            self.status = Phase4PreflightStatus.BLOCKED_EXTERNAL_DEPENDENCY
        else:
            self.status = Phase4PreflightStatus.PASSED
        return self

    def canonical_json(self) -> str:
        """Return stable JSON suitable for evidence files and comparisons."""

        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


__all__ = [
    "PHYSICAL_GATE_IDS",
    "SOFTWARE_GATE_IDS",
    "Phase4Gate",
    "Phase4GateKind",
    "Phase4GateStatus",
    "Phase4PreflightReport",
    "Phase4PreflightStatus",
]
