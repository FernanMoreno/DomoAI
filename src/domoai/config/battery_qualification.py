"""Server-owned qualification evidence for physical battery dispatch."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel
from domoai.optimizer.providers import DispatchableBatteryBinding

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
