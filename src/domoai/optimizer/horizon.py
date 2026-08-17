"""Shared, timezone-aware optimization horizon contract."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel


class Horizon(StrictModel):
    start: datetime
    end: datetime
    resolution_minutes: int = Field(gt=0)
    timezone: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> Horizon:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("horizon timestamps must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("horizon end must be after start")
        seconds = (
            self.end.astimezone(UTC) - self.start.astimezone(UTC)
        ).total_seconds()
        if seconds % (self.resolution_minutes * 60) != 0:
            raise ValueError("horizon duration must be divisible by resolution_minutes")
        return self

    @property
    def slots(self) -> int:
        seconds = (
            self.end.astimezone(UTC) - self.start.astimezone(UTC)
        ).total_seconds()
        return int(seconds // (self.resolution_minutes * 60))
