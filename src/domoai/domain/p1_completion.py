"""Strict semantic contracts shared by the remaining P1 connection flows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from .models import StrictModel


class CommissioningMethod(StrEnum):
    QR = "qr"
    MANUAL = "manual"


class CommissioningState(StrEnum):
    CREATED = "created"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CLEANED = "cleaned"


class MatterCommissioningSession(StrictModel):
    """Secret-free state for one bounded commissioning attempt."""

    session_id: str = Field(min_length=1, max_length=128)
    connection_id: str = Field(min_length=1, max_length=256)
    method: CommissioningMethod
    state: CommissioningState = CommissioningState.CREATED
    deadline: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confirmation_required: bool = True
    cleanup_done: bool = False

    @model_validator(mode="after")
    def validate_lifecycle(self) -> MatterCommissioningSession:
        for value in (self.created_at, self.deadline):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("commissioning timestamps must be timezone-aware")
        if self.deadline <= self.created_at:
            raise ValueError("deadline must be after created_at")
        if self.deadline - self.created_at > timedelta(hours=1):
            raise ValueError("commissioning deadline exceeds the one-hour bound")
        if self.state in {
            CommissioningState.COMPLETED,
            CommissioningState.FAILED,
            CommissioningState.EXPIRED,
            CommissioningState.CLEANED,
        } and not self.cleanup_done and self.state is CommissioningState.CLEANED:
            raise ValueError("cleaned commissioning sessions must set cleanup_done")
        return self


class ModbusDeviceIdentity(StrictModel):
    """Read Device Identification result with no raw credential-bearing data."""

    host_ref: str = Field(min_length=1, max_length=128)
    unit_id: int = Field(strict=True, ge=1, le=247)
    vendor_name: str = Field(default="", max_length=128)
    product_code: str = Field(default="", max_length=128)
    revision: str = Field(default="", max_length=128)
    read_device_id_code: int | None = Field(default=None, strict=True, ge=1, le=4)
    complete: bool = False
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_identity(self) -> ModbusDeviceIdentity:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Modbus identity timestamp must be timezone-aware")
        if self.complete and not (self.vendor_name and self.product_code and self.revision):
            raise ValueError("complete Modbus identity requires vendor, product and revision")
        return self


class ProfileResolutionStatus(StrEnum):
    MATCHED = "matched"
    PROFILE_REQUIRED = "profile_required"
    INCOMPATIBLE = "incompatible"


class ModbusProfileResolution(StrictModel):
    """Deterministic profile selection result; unknown means no active mapping."""

    status: ProfileResolutionStatus
    identity: ModbusDeviceIdentity
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    candidate_profile_ids: tuple[str, ...] = ()
    diagnostic: str | None = Field(default=None, min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_resolution(self) -> ModbusProfileResolution:
        if len(set(self.candidate_profile_ids)) != len(self.candidate_profile_ids):
            raise ValueError("candidate profile IDs must be unique")
        if self.status is ProfileResolutionStatus.MATCHED and self.profile_id is None:
            raise ValueError("matched profile resolution requires profile_id")
        if self.status is not ProfileResolutionStatus.MATCHED and self.profile_id is not None:
            raise ValueError("unmatched profile resolution cannot select a profile")
        return self


def validate_secret_safe_document(value: Any, *, field_name: str) -> None:
    """Validate a JSON document before it is stored as a connection artifact."""

    sensitive = ("token", "password", "secret", "credential", "authorization", "private_key")

    def visit(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field_name} keys must be strings")
                normalized = key.casefold().replace("-", "_")
                if any(fragment in normalized for fragment in sensitive):
                    raise ValueError(f"{field_name} contains sensitive fields")
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
        elif item is None or isinstance(item, (str, int, float, bool)):
            if isinstance(item, str) and any(fragment in item.casefold() for fragment in sensitive):
                raise ValueError(f"{field_name} contains sensitive material")
        else:
            raise ValueError(f"{field_name} must contain JSON values")

    visit(value, field_name)


__all__ = [
    "CommissioningMethod",
    "CommissioningState",
    "MatterCommissioningSession",
    "ModbusDeviceIdentity",
    "ModbusProfileResolution",
    "ProfileResolutionStatus",
    "validate_secret_safe_document",
]
