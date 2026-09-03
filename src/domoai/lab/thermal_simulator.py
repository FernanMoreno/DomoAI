"""Deterministic, transport-independent thermostat model for the local lab.

Mirrors `water_consumption_simulator.py` (spec 165 data-model.md): a
`Clock`-injectable state machine with explicit ticks and lab-harness
`set_hvac_mode`/`set_exterior_temperature`/`set_fault` controls. Applies the
same RC thermal recurrence the CP-SAT optimizer reasons about
(`src/domoai/optimizer/cp_sat.py`'s `_optimize_energy`, research.md
Decision 1) so the lab fixture and the optimizer agree on physics -- here
evaluated directly in floating point (no CP-SAT integer scaling/rounding-
tolerance concerns apply outside the solver's own integer domain).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from domoai.runtime.clock import Clock, SystemClock

ThermostatFault = Literal["stale", "unavailable", "rejected"]
HVACMode = Literal["heat", "cool", "off"]


@dataclass(frozen=True)
class ThermalSimulationProfile:
    provider_id: str
    device_id: str
    capacitance_kwh_per_c: float
    ua_kw_per_c: float
    initial_temperature_c: float
    initial_exterior_temperature_c: float
    max_heat_kw: float
    max_cool_kw: float
    heating_cop: float
    cooling_cop: float
    tick_seconds: float = 60.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ThermalSimulationProfile:
        expected = set(cls.__dataclass_fields__)
        unknown = set(payload) - expected
        if unknown:
            raise ValueError(f"unknown thermal profile fields: {sorted(unknown)}")
        profile = cls(**payload)
        profile.validate()
        return profile

    def validate(self) -> None:
        if not self.provider_id.strip() or not self.device_id.strip():
            raise ValueError("thermal provider_id and device_id are required")
        finite = (
            self.capacitance_kwh_per_c,
            self.ua_kw_per_c,
            self.initial_temperature_c,
            self.initial_exterior_temperature_c,
            self.max_heat_kw,
            self.max_cool_kw,
            self.heating_cop,
            self.cooling_cop,
            self.tick_seconds,
        )
        if any(not math.isfinite(value) for value in finite):
            raise ValueError("thermal profile values must be finite")
        if self.capacitance_kwh_per_c <= 0:
            raise ValueError("thermal capacitance_kwh_per_c must be positive")
        if self.ua_kw_per_c <= 0:
            raise ValueError("thermal ua_kw_per_c must be positive")
        if self.max_heat_kw < 0 or self.max_cool_kw < 0:
            raise ValueError("thermal max_heat_kw/max_cool_kw must not be negative")
        if self.heating_cop <= 0 or self.cooling_cop <= 0:
            raise ValueError("thermal heating_cop/cooling_cop must be positive")
        if self.tick_seconds <= 0:
            raise ValueError("thermal tick_seconds must be positive")


@dataclass(frozen=True)
class ThermalSimulationState:
    schema_version: str
    provider_id: str
    device_id: str
    indoor_temperature_c: float
    exterior_temperature_c: float
    hvac_mode: HVACMode
    hvac_power_kw: float
    available: bool
    fault: ThermostatFault | None
    observed_at: datetime
    revision: int

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["observed_at"] = self.observed_at.isoformat()
        return result


class ThermalSimulator:
    """State machine with explicit ticks and lab-harness mode/fault control."""

    def __init__(self, profile: ThermalSimulationProfile, *, clock: Clock | None = None) -> None:
        profile.validate()
        self.profile = profile
        self._clock = clock or SystemClock()
        self._fault: ThermostatFault | None = None
        self._indoor_temperature_c = profile.initial_temperature_c
        self._exterior_temperature_c = profile.initial_exterior_temperature_c
        self._hvac_mode: HVACMode = "off"
        self._revision = 0
        self._observed_at = self._clock.now()

    def _hvac_power_kw(self) -> float:
        if self._hvac_mode == "heat":
            return self.profile.max_heat_kw
        if self._hvac_mode == "cool":
            return self.profile.max_cool_kw
        return 0.0

    def snapshot(self) -> ThermalSimulationState:
        return ThermalSimulationState(
            schema_version="v1",
            provider_id=self.profile.provider_id,
            device_id=self.profile.device_id,
            indoor_temperature_c=round(self._indoor_temperature_c, 9),
            exterior_temperature_c=round(self._exterior_temperature_c, 9),
            hvac_mode=self._hvac_mode,
            hvac_power_kw=round(self._hvac_power_kw(), 9),
            available=self._fault != "unavailable",
            fault=self._fault,
            observed_at=self._observed_at,
            revision=self._revision,
        )

    def set_hvac_mode(self, mode: HVACMode) -> ThermalSimulationState:
        if mode not in ("heat", "cool", "off"):
            raise ValueError("thermal hvac_mode must be one of heat/cool/off")
        if self._fault == "unavailable":
            raise ConnectionError("thermal simulator is unavailable")
        self._hvac_mode = mode
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def set_exterior_temperature(self, value: float) -> ThermalSimulationState:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("thermal exterior temperature must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError("thermal exterior temperature must be finite")
        if self._fault == "unavailable":
            raise ConnectionError("thermal simulator is unavailable")
        self._exterior_temperature_c = float(value)
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def tick(self, seconds: float | None = None) -> ThermalSimulationState:
        duration = self.profile.tick_seconds if seconds is None else float(seconds)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("thermal tick duration must be finite and non-negative")
        if self._fault == "unavailable":
            raise ConnectionError("thermal simulator is unavailable")
        dt_hours = duration / 3600
        heat_kw = self.profile.max_heat_kw if self._hvac_mode == "heat" else 0.0
        cool_kw = self.profile.max_cool_kw if self._hvac_mode == "cool" else 0.0
        ua_loss = self.profile.ua_kw_per_c * (
            self._indoor_temperature_c - self._exterior_temperature_c
        )
        delta_c = (
            dt_hours
            * (
                heat_kw * self.profile.heating_cop
                - cool_kw * self.profile.cooling_cop
                - ua_loss
            )
            / self.profile.capacitance_kwh_per_c
        )
        self._indoor_temperature_c += delta_c
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def set_fault(self, fault: ThermostatFault | None) -> ThermalSimulationState:
        self._fault = fault
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "ThermalSimulationProfile",
    "ThermalSimulationState",
    "ThermalSimulator",
]
