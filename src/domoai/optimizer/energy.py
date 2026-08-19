"""Versioned, read-only energy context for deterministic optimization."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel
from domoai.optimizer.horizon import Horizon


class TariffPoint(StrictModel):
    slot: int = Field(ge=0)
    # Wholesale market-backed tariffs can be negative. Providers must convert
    # their source units to canonical EUR/kWh (or the declared currency) before
    # returning this model; physical import/export constraints are separate.
    price_per_kwh: float
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ConfidenceBand(StrictModel):
    low: float = Field(ge=0)
    high: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> ConfidenceBand:
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        return self


class SolarForecastPoint(StrictModel):
    slot: int = Field(ge=0)
    power: float = Field(ge=0)
    unit: Literal["kW"] = "kW"
    confidence: ConfidenceBand | None = None

    @model_validator(mode="after")
    def validate_confidence(self) -> SolarForecastPoint:
        if self.confidence is not None and not (
            self.confidence.low <= self.power <= self.confidence.high
        ):
            raise ValueError("power must fall within its confidence band")
        return self


class BaseLoadPoint(StrictModel):
    slot: int = Field(ge=0)
    power: float = Field(ge=0)
    unit: Literal["kW"] = "kW"
    confidence: ConfidenceBand | None = None

    @model_validator(mode="after")
    def validate_confidence(self) -> BaseLoadPoint:
        if self.confidence is not None and not (
            self.confidence.low <= self.power <= self.confidence.high
        ):
            raise ValueError("power must fall within its confidence band")
        return self


class BatteryProfile(StrictModel):
    capacity_kwh: float = Field(gt=0)
    initial_soc_kwh: float = Field(ge=0)
    min_soc_kwh: float = Field(ge=0)
    max_soc_kwh: float = Field(gt=0)
    max_charge_kw: float = Field(ge=0)
    max_discharge_kw: float = Field(ge=0)
    charge_efficiency: float = Field(gt=0, le=1)
    discharge_efficiency: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_state_domain(self) -> BatteryProfile:
        if self.min_soc_kwh > self.initial_soc_kwh:
            raise ValueError("initial_soc_kwh must be greater than or equal to min_soc_kwh")
        if self.initial_soc_kwh > self.max_soc_kwh:
            raise ValueError("initial_soc_kwh must be less than or equal to max_soc_kwh")
        if self.max_soc_kwh > self.capacity_kwh:
            raise ValueError("max_soc_kwh must be less than or equal to capacity_kwh")
        if self.min_soc_kwh > self.max_soc_kwh:
            raise ValueError("min_soc_kwh must be less than or equal to max_soc_kwh")
        return self


class EnergyContext(StrictModel):
    """Complete external energy context for exactly one optimization horizon."""

    schema_version: Literal["v1"] = "v1"
    horizon: Horizon
    tariffs: list[TariffPoint]
    solar_forecast: list[SolarForecastPoint]
    base_load_forecast: list[BaseLoadPoint] | None = None
    export_tariffs: list[TariffPoint] | None = None
    battery: BatteryProfile | None = None
    source_revision: str = Field(min_length=1)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_series(self) -> EnergyContext:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        expected = list(range(self.horizon.slots))
        SeriesEntry = tuple[
            str, list[TariffPoint] | list[SolarForecastPoint] | list[BaseLoadPoint]
        ]
        series: list[SeriesEntry] = [
            ("tariffs", self.tariffs),
            ("solar_forecast", self.solar_forecast),
        ]
        if self.base_load_forecast is not None:
            series.append(("base_load_forecast", self.base_load_forecast))
        if self.export_tariffs is not None:
            series.append(("export_tariffs", self.export_tariffs))
        for name, points in series:
            slots = [point.slot for point in points]
            if len(points) != self.horizon.slots:
                raise ValueError(
                    f"{name} must contain exactly one point for every horizon slot"
                )
            if len(set(slots)) != len(slots):
                raise ValueError(f"{name} must not contain duplicate slots")
            if slots != expected:
                if sorted(slots) == expected:
                    raise ValueError(f"{name} slots must be ordered")
                raise ValueError(f"{name} slots must cover the horizon exactly")
        if len({point.confidence is not None for point in self.solar_forecast}) > 1:
            raise ValueError(
                "solar_forecast confidence bounds must be provided for every slot or none"
            )
        if self.base_load_forecast is not None and (
            len({point.confidence is not None for point in self.base_load_forecast}) > 1
        ):
            raise ValueError(
                "base_load_forecast confidence bounds must be provided for every slot or none"
            )
        return self


class StaticEnergyContextProvider:
    """Deterministic provider used by local deployments and acceptance tests."""

    def __init__(self, context: EnergyContext) -> None:
        self._context = context

    def get_context(self, horizon: Horizon) -> EnergyContext:
        if self._context.horizon != horizon:
            raise ValueError("energy context horizon does not match requested horizon")
        return self._context
