"""Server-owned qualification evidence for physical battery dispatch."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
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
MANUAL_HIL_CHECKS = frozenset({"native_scheduler_conflict", "restart_no_replay"})


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
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    runtime_binding_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    takeover_evidence_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    qualification_expires_at: datetime | None = None
    hardware_identity_observed: bool = False
    firmware_identity_observed: bool = False
    identity_observed_at: datetime | None = None
    identity_evidence_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    manual_check_status: dict[
        str, Literal["verified", "not_verified", "not_exercised", "not_applicable"]
    ] = Field(default_factory=dict)

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
        for field_name, value in (
            ("qualification_expires_at", self.qualification_expires_at),
            ("identity_observed_at", self.identity_observed_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        unknown_manual_status = set(self.manual_check_status) - REQUIRED_HIL_CHECKS
        if unknown_manual_status:
            raise ValueError("manual check status references unknown checks")
        return self

    def qualifies(
        self,
        binding: DispatchableBatteryBinding,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(hours=24),
    ) -> bool:
        """Return true only for complete, fresh, scope-bound evidence.

        A fixture report may be useful for diagnostics but cannot silently
        become production authority: provider/runtime/takeover provenance and
        structured manual verification are required here.
        """

        if self.status != "passed" or self.profile_digest != battery_binding_digest(binding):
            return False
        if self.provider_id != binding.provider_id:
            return False
        if self.runtime_binding_digest != self.profile_digest:
            return False
        if self.takeover_evidence_digest is None:
            return False
        if not self.test_software_version:
            return False
        if not self.hardware_identity_observed or not self.firmware_identity_observed:
            return False
        if self.identity_observed_at is None:
            return False
        if self.identity_evidence_digest != battery_identity_digest(
            hardware_id=self.hardware_id,
            firmware_version=self.firmware_version,
            provider_id=self.provider_id,
            profile_digest=self.profile_digest,
            observed_at=self.identity_observed_at,
        ):
            return False
        if any(self.manual_check_status.get(check) != "verified" for check in MANUAL_HIL_CHECKS):
            return False
        if any(
            self.manual_check_status.get(check) != "verified"
            for check in REQUIRED_HIL_CHECKS
            if check in self.manual_attestations
        ):
            return False
        if any(
            marker in note.lower()
            for note in self.manual_attestations.values()
            for marker in ("not exercised", "not tested", "not verified", "not run")
        ):
            return False
        current = now or datetime.now(UTC)
        if current < self.completed_at or current - self.completed_at > max_age:
            return False
        if current < self.identity_observed_at or current - self.identity_observed_at > max_age:
            return False
        if self.identity_observed_at > self.completed_at:
            return False
        if self.qualification_expires_at is not None and current >= self.qualification_expires_at:
            return False
        return True


def battery_binding_digest(binding: DispatchableBatteryBinding) -> str:
    canonical = json.dumps(
        binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def battery_identity_digest(
    *,
    hardware_id: str,
    firmware_version: str,
    provider_id: str,
    profile_digest: str,
    observed_at: datetime,
) -> str:
    """Digest provider-observed identity and its exact qualification scope."""

    payload = {
        "hardware_id": hardware_id,
        "firmware_version": firmware_version,
        "provider_id": provider_id,
        "profile_digest": profile_digest,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
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
    "MANUAL_HIL_CHECKS",
    "battery_binding_digest",
    "battery_identity_digest",
    "load_battery_hil_evidence",
]
