"""Canonical solar installation configuration."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel


class SolarInstallationProfile(StrictModel):
    """Versioned, credential-free metadata for one photovoltaic installation."""

    schema_version: Literal["v1"] = "v1"
    profile_id: str = Field(
        default="home", pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64
    )
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    installed_kwp: float = Field(gt=0)
    tilt: float = Field(ge=0, le=90)
    azimuth: float = Field(ge=-180, le=180)
    performance_ratio: float = Field(gt=0, le=1)
    inverter_ac_max_kw: float | None = Field(default=None, gt=0)
    timezone: str = Field(default="Europe/Madrid", min_length=1)
    source_id: str = Field(
        default="operator_config", pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64
    )
    source_revision: str = Field(
        default="v1", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
    )
    captured_at: datetime | None = None

    @model_validator(mode="after")
    def validate_profile(self) -> SolarInstallationProfile:
        numeric_values = (
            self.latitude,
            self.longitude,
            self.installed_kwp,
            self.tilt,
            self.azimuth,
            self.performance_ratio,
            self.inverter_ac_max_kw,
        )
        if any(value is not None and not math.isfinite(value) for value in numeric_values):
            raise ValueError("solar profile numeric values must be finite")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        if self.captured_at is not None and self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        return self


__all__ = ["SolarInstallationProfile"]
