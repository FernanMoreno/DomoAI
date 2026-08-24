"""Server-owned qualification evidence for physical battery dispatch."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from domoai.domain.energy import DispatchableBatteryBinding
from domoai.domain.models import StrictModel

REQUIRED_HIL_CHECKS = frozenset(
    {
        "identity",
        "writable_routes",
        "takeover_baseline",
        "charge_feedback",
        "discharge_feedback",
        "stop_feedback",
        "polarity",
        "soc_reconciliation",
        "native_scheduler_conflict",
        "restart_no_replay",
    }
)


class BatteryQualificationError(ValueError):
    """Raised when a physical battery qualification artifact is unsafe."""


class BatteryHILEvidence(StrictModel):
    schema_version: Literal["v1"] = "v1"
    status: Literal["passed", "failed"]
    profile_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    hardware_id: str = Field(min_length=1, max_length=200)
    firmware_version: str = Field(min_length=1, max_length=200)
    completed_at: datetime
    checks: dict[str, bool] = Field(min_length=1)
    run_id: str = Field(min_length=1, max_length=128)
    # Additive provenance fields (spec 146): distinguish a check the runner
    # actually observed from a real adapter call (`observations`) from one
    # an operator attests to separately because it cannot be derived from a
    # single automated run (`manual_attestations`) -- e.g. verifying no
    # native scheduler conflict, or that a restart did not replay a command,
    # both require multi-phase/out-of-band verification a single CLI
    # invocation cannot self-certify.
    test_software_version: str | None = Field(default=None, max_length=200)
    observations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    manual_attestations: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_evidence(self) -> BatteryHILEvidence:
        if self.completed_at.tzinfo is None:
            raise ValueError("battery HIL completed_at must be timezone-aware")
        unknown = set(self.checks) - REQUIRED_HIL_CHECKS
        if unknown:
            raise ValueError("battery HIL evidence contains unknown checks")
        missing = REQUIRED_HIL_CHECKS - set(self.checks)
        if missing:
            raise ValueError("battery HIL evidence is missing required checks")
        if self.status == "passed" and not all(self.checks.values()):
            raise ValueError("passed battery HIL evidence must pass every required check")
        unknown_manual = set(self.manual_attestations) - REQUIRED_HIL_CHECKS
        if unknown_manual:
            raise ValueError("battery HIL manual attestations reference unknown checks")
        return self

    def qualifies(self, binding: DispatchableBatteryBinding) -> bool:
        return self.status == "passed" and self.profile_digest == battery_binding_digest(binding)


def battery_binding_digest(binding: DispatchableBatteryBinding) -> str:
    canonical = json.dumps(
        binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_battery_hil_evidence(path: Path) -> BatteryHILEvidence:
    try:
        return BatteryHILEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise BatteryQualificationError(
            "battery HIL evidence is unavailable or not valid v1 JSON"
        ) from error


__all__ = [
    "BatteryHILEvidence",
    "BatteryQualificationError",
    "REQUIRED_HIL_CHECKS",
    "battery_binding_digest",
    "load_battery_hil_evidence",
]
