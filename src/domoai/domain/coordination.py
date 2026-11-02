"""Provider-neutral lease and fencing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from domoai.domain.models import StrictModel


class LeaseScope(StrictModel):
    """The smallest authority scope that may have one physical owner."""

    tenant_id: str = Field(min_length=1, max_length=200)
    household_id: str = Field(min_length=1, max_length=200)
    deployment_id: str = Field(min_length=1, max_length=200)


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"
    UNKNOWN = "unknown"


class FencingToken(StrictModel):
    """Non-secret proof carried to the physical boundary."""

    scope: LeaseScope
    owner_id: str = Field(min_length=1, max_length=200)
    epoch: int = Field(gt=0)
    lease_id: str = Field(min_length=1, max_length=200)
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_window(self) -> FencingToken:
        if self.expires_at <= self.issued_at:
            raise ValueError("lease expiry must be after issue time")
        return self


class LeaseCoordinator(Protocol):
    async def acquire(
        self, scope: LeaseScope, *, owner_id: str, ttl_seconds: float
    ) -> FencingToken: ...

    async def renew(self, token: FencingToken) -> FencingToken: ...

    async def release(self, token: FencingToken) -> None: ...

    async def validate(self, token: FencingToken) -> None: ...


class LeaseUnavailable(RuntimeError):
    """The scope is currently owned by another live host."""


class FencingViolation(RuntimeError):
    """A physical operation presented no longer-valid ownership proof."""


class PhysicalIntentStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    ACKNOWLEDGED = "acknowledged"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class PhysicalIntent(StrictModel):
    """Durable idempotency record for one household physical intention."""

    tenant_id: str = Field(min_length=1, max_length=200)
    household_id: str = Field(min_length=1, max_length=200)
    deployment_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=200)
    command_id: str = Field(min_length=1, max_length=200)
    fencing_epoch: int = Field(gt=0)
    status: PhysicalIntentStatus = PhysicalIntentStatus.PREPARED
    owner_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        exclude_if=lambda value: value is None,
    )
    execution_attempt_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        exclude_if=lambda value: value is None,
    )
    lease_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        exclude_if=lambda value: value is None,
    )
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("physical intent timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_dispatch_identity(self) -> PhysicalIntent:
        if self.status is PhysicalIntentStatus.DISPATCHING and (
            self.owner_id is None or self.execution_attempt_id is None
        ):
            raise ValueError("dispatching physical intents require owner and execution attempt")
        return self


class PhysicalIntentClaimDecision(StrEnum):
    ACQUIRED = "acquired"
    REPLAY = "replay"
    IN_FLIGHT = "in_flight"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class PhysicalIntentClaim:
    decision: PhysicalIntentClaimDecision
    intent: PhysicalIntent
