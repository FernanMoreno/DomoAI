"""Server-declared residual demand reserves, with no meter extrapolation or I/O at solve time."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel
from domoai.optimizer.energy import BaseLoadPoint, BaseLoadSource
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.providers import BaseLoadForecastSeries, _provider_error


class DeclaredBaseLoadProfile(StrictModel):
    """A household assumption, not execution authority or a measured power guarantee."""

    schema_version: Literal["v1"] = "v1"
    tenant_id: str = Field(min_length=1, max_length=128)
    household_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64)
    source_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$", max_length=128)
    declared_at: datetime
    valid_from: datetime
    valid_until: datetime
    power: float = Field(ge=0, allow_inf_nan=False, strict=True)
    unit: Literal["kW"]
    kind: Literal["declared_reserve"]
    scope: Literal["uncontrolled_demand"]

    @model_validator(mode="after")
    def validate_dates(self) -> DeclaredBaseLoadProfile:
        self.source()
        return self

    def source(self) -> BaseLoadSource:
        return BaseLoadSource(
            source_id=self.source_id,
            source_revision=self.source_revision,
            observed_at=self.declared_at,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
            kind=self.kind,
            scope=self.scope,
        )


class DeclaredBaseLoadProvider:
    """Materialize a scoped constant reserve only inside its declared horizon window."""

    def __init__(
        self,
        profile: DeclaredBaseLoadProfile,
        *,
        tenant_id: str,
        household_id: str,
    ) -> None:
        if (profile.tenant_id, profile.household_id) != (tenant_id, household_id):
            raise _provider_error(
                "base_load",
                code="base_load_scope_mismatch",
                message="base reserve profile scope does not match the runtime",
            )
        # Own a validated snapshot; callers cannot change the profile after binding.
        self._profile = DeclaredBaseLoadProfile.model_validate_json(profile.model_dump_json())
        self.provider_id = profile.source_id
        self._revision = hashlib.sha256(self._profile.model_dump_json().encode()).hexdigest()

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        tenant_id: str,
        household_id: str,
    ) -> DeclaredBaseLoadProvider:
        try:
            profile = DeclaredBaseLoadProfile.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise _provider_error(
                "base_load",
                code="provider_invalid",
                message="base reserve profile is invalid or unavailable",
            ) from error
        return cls(profile, tenant_id=tenant_id, household_id=household_id)

    def get_forecast(self, horizon: Horizon) -> BaseLoadForecastSeries:
        source = self._profile.source()
        try:
            source.validate_horizon(horizon)
        except ValueError as error:
            raise _provider_error(
                self.provider_id,
                code="horizon_mismatch",
                message="base reserve does not cover the requested horizon",
            ) from error
        return BaseLoadForecastSeries(
            **source.model_dump(exclude={"source_revision"}),
            source_revision=self._revision,
            horizon=horizon,
            points=[
                BaseLoadPoint(slot=slot, power=self._profile.power) for slot in range(horizon.slots)
            ],
        )
