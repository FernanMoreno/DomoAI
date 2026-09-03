"""Versioned, read-only energy context for deterministic optimization."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.energy import (
    SOC_OBSERVATION_TOLERANCE_KWH,
    BatteryActuator,
    BatteryCapacityEvidence,
    BatteryControlPolicy,
    BatteryProfile,
    BatterySocConversionEvidence,
    BatterySocObservation,
    DispatchableBatteryBinding,
    EVActuator,
    EVChargingBinding,
    HVACActuator,
    NominalCapacityTrustPolicy,
    ThermalProfile,
)
from domoai.domain.models import SourceRef, StrictModel
from domoai.domain.provider import MeasurementQuality
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


class ExteriorTemperaturePoint(StrictModel):
    slot: int = Field(ge=0)
    temperature_c: float
    unit: Literal["degC"] = "degC"


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


class EVState(StrictModel):
    """Canonical provider-observed EV state used for executable planning."""

    schema_version: Literal["v1"] = "v1"
    device_id: str = Field(min_length=1)
    connected: bool
    soc_kwh: float = Field(ge=0)
    capacity_kwh: float = Field(gt=0)
    max_charge_kw: float = Field(ge=0)
    departure_at: datetime | None = None
    observed_at: datetime
    received_at: datetime
    source_revision: str = Field(min_length=1)
    source_ref: SourceRef
    quality: MeasurementQuality = MeasurementQuality.GOOD

    @model_validator(mode="after")
    def validate_state(self) -> EVState:
        if self.soc_kwh > self.capacity_kwh:
            raise ValueError("EV SOC must not exceed capacity")
        if self.observed_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("EV state timestamps must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("EV received_at must be greater than or equal to observed_at")
        if self.source_ref.adapter_id.strip() == "":
            raise ValueError("EV state source reference must identify an adapter")
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
    ev_states: list[EVState] = Field(default_factory=list)
    thermal: ThermalProfile | None = None
    exterior_temperature_forecast: list[ExteriorTemperaturePoint] | None = None
    source_revision: str = Field(min_length=1)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_series(self) -> EnergyContext:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        ev_devices = [state.device_id for state in self.ev_states]
        if len(ev_devices) != len(set(ev_devices)):
            raise ValueError("ev_states must contain one state per device")
        expected = list(range(self.horizon.slots))
        SeriesEntry = tuple[
            str,
            list[TariffPoint]
            | list[SolarForecastPoint]
            | list[BaseLoadPoint]
            | list[ExteriorTemperaturePoint],
        ]
        series: list[SeriesEntry] = [
            ("tariffs", self.tariffs),
            ("solar_forecast", self.solar_forecast),
        ]
        if self.base_load_forecast is not None:
            series.append(("base_load_forecast", self.base_load_forecast))
        if self.export_tariffs is not None:
            series.append(("export_tariffs", self.export_tariffs))
        if self.exterior_temperature_forecast is not None:
            series.append(("exterior_temperature_forecast", self.exterior_temperature_forecast))
        for name, points in series:
            slots = [point.slot for point in points]
            if len(points) != self.horizon.slots:
                raise ValueError(f"{name} must contain exactly one point for every horizon slot")
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


__all__ = [
    "SOC_OBSERVATION_TOLERANCE_KWH",
    "BatteryActuator",
    "BatteryCapacityEvidence",
    "BatteryControlPolicy",
    "BatteryProfile",
    "BatterySocConversionEvidence",
    "BatterySocObservation",
    "DispatchableBatteryBinding",
    "EVActuator",
    "EVChargingBinding",
    "HVACActuator",
    "NominalCapacityTrustPolicy",
    "ThermalProfile",
    "BaseLoadPoint",
    "ConfidenceBand",
    "EnergyContext",
    "EVState",
    "ExteriorTemperaturePoint",
    "SolarForecastPoint",
    "StaticEnergyContextProvider",
    "TariffPoint",
]
