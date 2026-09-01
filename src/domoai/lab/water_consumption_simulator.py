"""Deterministic, transport-independent water meter model for the local lab.

Mirrors `battery_simulator.py`/`ev_charging_simulator.py` (spec 163
research.md): purely read-only, no commands -- `set_flow_rate`/`set_fault`
are lab-harness controls for driving the simulation, not projections of a
canonical device command (there is no writable capability for a water
meter). This module deliberately contains no adapter or production-authority
logic. It is the single source of simulated physical state used by lab
projections.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from domoai.runtime.clock import Clock, SystemClock

WaterMeterFault = Literal["stale", "unavailable", "rejected"]


@dataclass(frozen=True)
class WaterConsumptionSimulationProfile:
    provider_id: str
    device_id: str
    initial_total_volume_l: float = 0.0
    initial_flow_rate_lpm: float = 0.0
    tick_seconds: float = 1.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WaterConsumptionSimulationProfile:
        expected = set(cls.__dataclass_fields__)
        unknown = set(payload) - expected
        if unknown:
            raise ValueError(f"unknown water consumption profile fields: {sorted(unknown)}")
        profile = cls(**payload)
        profile.validate()
        return profile

    def validate(self) -> None:
        if not self.provider_id.strip() or not self.device_id.strip():
            raise ValueError("water consumption provider_id and device_id are required")
        finite = (self.initial_total_volume_l, self.initial_flow_rate_lpm, self.tick_seconds)
        if any(not math.isfinite(value) for value in finite):
            raise ValueError("water consumption profile values must be finite")
        if self.tick_seconds <= 0:
            raise ValueError("water consumption tick_seconds must be positive")
        if self.initial_total_volume_l < 0:
            raise ValueError("water consumption initial_total_volume_l must not be negative")
        if self.initial_flow_rate_lpm < 0:
            raise ValueError("water consumption initial_flow_rate_lpm must not be negative")


@dataclass(frozen=True)
class WaterConsumptionSimulationState:
    schema_version: str
    provider_id: str
    device_id: str
    flow_rate_lpm: float
    total_volume_l: float
    available: bool
    fault: WaterMeterFault | None
    observed_at: datetime
    revision: int

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["observed_at"] = self.observed_at.isoformat()
        return result


class WaterConsumptionSimulator:
    """State machine with explicit ticks and lab-harness flow/fault control."""

    def __init__(
        self, profile: WaterConsumptionSimulationProfile, *, clock: Clock | None = None
    ) -> None:
        profile.validate()
        self.profile = profile
        self._clock = clock or SystemClock()
        self._fault: WaterMeterFault | None = None
        self._total_volume_l = profile.initial_total_volume_l
        self._flow_rate_lpm = profile.initial_flow_rate_lpm
        self._revision = 0
        self._observed_at = self._clock.now()

    def snapshot(self) -> WaterConsumptionSimulationState:
        return WaterConsumptionSimulationState(
            schema_version="v1",
            provider_id=self.profile.provider_id,
            device_id=self.profile.device_id,
            flow_rate_lpm=round(self._flow_rate_lpm, 9),
            total_volume_l=round(self._total_volume_l, 9),
            available=self._fault != "unavailable",
            fault=self._fault,
            observed_at=self._observed_at,
            revision=self._revision,
        )

    def set_flow_rate(self, value: float) -> WaterConsumptionSimulationState:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("water consumption flow rate must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError("water consumption flow rate must be finite")
        if value < 0:
            raise ValueError("water consumption flow rate must not be negative")
        if self._fault == "unavailable":
            raise ConnectionError("water consumption simulator is unavailable")
        self._flow_rate_lpm = float(value)
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def tick(self, seconds: float | None = None) -> WaterConsumptionSimulationState:
        duration = self.profile.tick_seconds if seconds is None else float(seconds)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("water consumption tick duration must be finite and non-negative")
        if self._fault == "unavailable":
            raise ConnectionError("water consumption simulator is unavailable")
        self._total_volume_l += self._flow_rate_lpm * duration / 60
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def set_fault(self, fault: WaterMeterFault | None) -> WaterConsumptionSimulationState:
        self._fault = fault
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "WaterConsumptionSimulationProfile",
    "WaterConsumptionSimulationState",
    "WaterConsumptionSimulator",
]
