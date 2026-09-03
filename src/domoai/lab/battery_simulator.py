"""Deterministic, transport-independent battery model for the local lab.

This module deliberately contains no adapter or production-authority logic. It
is the single source of simulated physical state used by lab projections.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from domoai.runtime.clock import Clock, SystemClock

BatteryMode = Literal["idle", "charging", "discharging"]
BatteryFault = Literal["stale", "unavailable", "rejected"]


@dataclass(frozen=True)
class BatterySimulationProfile:
    provider_id: str
    device_id: str
    capacity_kwh: float
    initial_soc_kwh: float
    min_soc_kwh: float
    max_soc_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    tick_seconds: float = 1.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BatterySimulationProfile:
        expected = set(cls.__dataclass_fields__)
        unknown = set(payload) - expected
        missing = expected - set(payload) - {"tick_seconds"}
        if unknown:
            raise ValueError(f"unknown battery profile fields: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing battery profile fields: {sorted(missing)}")
        profile = cls(**payload)
        profile.validate()
        return profile

    def validate(self) -> None:
        if not self.provider_id.strip() or not self.device_id.strip():
            raise ValueError("battery provider_id and device_id are required")
        finite = (
            self.capacity_kwh,
            self.initial_soc_kwh,
            self.min_soc_kwh,
            self.max_soc_kwh,
            self.max_charge_kw,
            self.max_discharge_kw,
            self.charge_efficiency,
            self.discharge_efficiency,
            self.tick_seconds,
        )
        if any(not math.isfinite(value) for value in finite):
            raise ValueError("battery profile values must be finite")
        if self.capacity_kwh <= 0 or self.tick_seconds <= 0:
            raise ValueError("battery capacity and tick_seconds must be positive")
        if not 0 < self.charge_efficiency <= 1 or not 0 < self.discharge_efficiency <= 1:
            raise ValueError("battery efficiencies must be in (0, 1]")
        if not 0 <= self.min_soc_kwh <= self.initial_soc_kwh <= self.max_soc_kwh:
            raise ValueError("battery initial SOC must be inside min/max envelope")
        if self.max_soc_kwh > self.capacity_kwh:
            raise ValueError("battery max_soc_kwh cannot exceed capacity")
        if self.max_charge_kw < 0 or self.max_discharge_kw < 0:
            raise ValueError("battery power limits cannot be negative")


@dataclass(frozen=True)
class BatterySimulationState:
    schema_version: str
    provider_id: str
    device_id: str
    soc_kwh: float
    power_kw: float
    capacity_kwh: float
    mode: BatteryMode
    available: bool
    fault: BatteryFault | None
    observed_at: datetime
    revision: int

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["observed_at"] = self.observed_at.isoformat()
        return result


class BatterySimulator:
    """State machine with explicit ticks and idempotent commands."""

    def __init__(self, profile: BatterySimulationProfile, *, clock: Clock | None = None) -> None:
        profile.validate()
        self.profile = profile
        self._clock = clock or SystemClock()
        self._fault: BatteryFault | None = None
        self._soc_kwh = profile.initial_soc_kwh
        self._power_kw = 0.0
        self._mode: BatteryMode = "idle"
        self._revision = 0
        self._idempotency_keys: set[str] = set()
        self._observed_at = self._clock.now()

    def snapshot(self) -> BatterySimulationState:
        return BatterySimulationState(
            schema_version="v1",
            provider_id=self.profile.provider_id,
            device_id=self.profile.device_id,
            soc_kwh=round(self._soc_kwh, 9),
            power_kw=round(self._power_kw, 9),
            capacity_kwh=self.profile.capacity_kwh,
            mode=self._mode,
            available=self._fault != "unavailable",
            fault=self._fault,
            observed_at=self._observed_at,
            revision=self._revision,
        )

    def command(
        self, name: str, *, value: float | None = None, idempotency_key: str
    ) -> BatterySimulationState:
        if not idempotency_key.strip():
            raise ValueError("battery idempotency_key is required")
        if idempotency_key in self._idempotency_keys:
            return self.snapshot()
        if self._fault == "unavailable":
            raise ConnectionError("battery simulator is unavailable")
        if name == "stop_battery":
            if value is not None:
                raise ValueError("stop_battery does not accept a value")
            self._power_kw = 0.0
            self._mode = "idle"
        elif name in {"charge_battery", "discharge_battery"}:
            if value is None or isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError("battery command value must be finite")
            power = float(value)
            if power <= 0:
                raise ValueError("battery command value must be positive")
            if name == "charge_battery":
                if power > self.profile.max_charge_kw:
                    raise ValueError("battery command exceeds max_charge_kw")
                if self._soc_kwh >= self.profile.max_soc_kwh:
                    raise ValueError("battery is at maximum SOC")
                self._power_kw, self._mode = power, "charging"
            else:
                if power > self.profile.max_discharge_kw:
                    raise ValueError("battery command exceeds max_discharge_kw")
                if self._soc_kwh <= self.profile.min_soc_kwh:
                    raise ValueError("battery command would cross minimum SOC")
                self._power_kw, self._mode = -power, "discharging"
        else:
            raise ValueError(f"unsupported battery command: {name}")
        self._idempotency_keys.add(idempotency_key)
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def tick(self, seconds: float | None = None) -> BatterySimulationState:
        duration = self.profile.tick_seconds if seconds is None else float(seconds)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("battery tick duration must be finite and non-negative")
        if self._fault == "unavailable":
            raise ConnectionError("battery simulator is unavailable")
        if self._mode == "charging":
            delta = self._power_kw * self.profile.charge_efficiency * duration / 3600
        elif self._mode == "discharging":
            delta = self._power_kw / self.profile.discharge_efficiency * duration / 3600
        else:
            delta = 0.0
        self._soc_kwh = min(
            self.profile.max_soc_kwh,
            max(self.profile.min_soc_kwh, self._soc_kwh + delta),
        )
        if self._soc_kwh in {self.profile.min_soc_kwh, self.profile.max_soc_kwh}:
            self._power_kw = 0.0
            self._mode = "idle"
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def set_fault(self, fault: BatteryFault | None) -> BatterySimulationState:
        self._fault = fault
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["BatterySimulationProfile", "BatterySimulationState", "BatterySimulator"]
