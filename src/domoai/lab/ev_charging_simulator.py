"""Deterministic, transport-independent EV charger model for the local lab.

Mirrors `battery_simulator.py` (spec 162 research.md Decision 5): charge-only
(no discharge/V2G — no FR/US requests it), with connected/departure state in
place of a discharge mode. This module deliberately contains no adapter or
production-authority logic. It is the single source of simulated physical
state used by lab projections.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from domoai.runtime.clock import Clock, SystemClock

EVChargingMode = Literal["idle", "charging"]
EVChargingFault = Literal["stale", "unavailable", "rejected"]


@dataclass(frozen=True)
class EVChargingSimulationProfile:
    provider_id: str
    device_id: str
    capacity_kwh: float
    initial_soc_kwh: float
    max_charge_kw: float
    charge_efficiency: float
    initial_connected: bool = True
    initial_departure_at: datetime | None = None
    tick_seconds: float = 1.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EVChargingSimulationProfile:
        expected = set(cls.__dataclass_fields__)
        payload = dict(payload)
        departure = payload.get("initial_departure_at")
        if isinstance(departure, str):
            payload["initial_departure_at"] = datetime.fromisoformat(departure)
        unknown = set(payload) - expected
        missing = expected - set(payload) - {
            "tick_seconds",
            "initial_connected",
            "initial_departure_at",
        }
        if unknown:
            raise ValueError(f"unknown EV charging profile fields: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing EV charging profile fields: {sorted(missing)}")
        profile = cls(**payload)
        profile.validate()
        return profile

    def validate(self) -> None:
        if not self.provider_id.strip() or not self.device_id.strip():
            raise ValueError("EV charging provider_id and device_id are required")
        finite = (
            self.capacity_kwh,
            self.initial_soc_kwh,
            self.max_charge_kw,
            self.charge_efficiency,
            self.tick_seconds,
        )
        if any(not math.isfinite(value) for value in finite):
            raise ValueError("EV charging profile values must be finite")
        if self.capacity_kwh <= 0 or self.tick_seconds <= 0:
            raise ValueError("EV charging capacity and tick_seconds must be positive")
        if not 0 < self.charge_efficiency <= 1:
            raise ValueError("EV charging efficiency must be in (0, 1]")
        if self.max_charge_kw <= 0:
            raise ValueError("EV charging max_charge_kw must be positive")
        if not 0 <= self.initial_soc_kwh <= self.capacity_kwh:
            raise ValueError("EV charging initial SOC must be inside [0, capacity_kwh]")
        if self.initial_departure_at is not None and self.initial_departure_at.tzinfo is None:
            raise ValueError("EV charging initial_departure_at must be timezone-aware")


@dataclass(frozen=True)
class EVChargingSimulationState:
    schema_version: str
    provider_id: str
    device_id: str
    soc_kwh: float
    power_kw: float
    capacity_kwh: float
    connected: bool
    departure_at: datetime | None
    mode: EVChargingMode
    available: bool
    fault: EVChargingFault | None
    observed_at: datetime
    revision: int

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["observed_at"] = self.observed_at.isoformat()
        if self.departure_at is not None:
            result["departure_at"] = self.departure_at.isoformat()
        return result


class EVChargingSimulator:
    """State machine with explicit ticks and idempotent commands."""

    def __init__(
        self, profile: EVChargingSimulationProfile, *, clock: Clock | None = None
    ) -> None:
        profile.validate()
        self.profile = profile
        self._clock = clock or SystemClock()
        self._fault: EVChargingFault | None = None
        self._soc_kwh = profile.initial_soc_kwh
        self._power_kw = 0.0
        self._mode: EVChargingMode = "idle"
        self._connected = profile.initial_connected
        self._departure_at = profile.initial_departure_at
        self._revision = 0
        self._idempotency_keys: set[str] = set()
        self._observed_at = self._clock.now()

    def snapshot(self) -> EVChargingSimulationState:
        return EVChargingSimulationState(
            schema_version="v1",
            provider_id=self.profile.provider_id,
            device_id=self.profile.device_id,
            soc_kwh=round(self._soc_kwh, 9),
            power_kw=round(self._power_kw, 9),
            capacity_kwh=self.profile.capacity_kwh,
            connected=self._connected,
            departure_at=self._departure_at,
            mode=self._mode,
            available=self._fault != "unavailable",
            fault=self._fault,
            observed_at=self._observed_at,
            revision=self._revision,
        )

    def command(
        self, name: str, *, value: float | None = None, idempotency_key: str
    ) -> EVChargingSimulationState:
        if not idempotency_key.strip():
            raise ValueError("EV charging idempotency_key is required")
        if idempotency_key in self._idempotency_keys:
            return self.snapshot()
        if self._fault == "unavailable":
            raise ConnectionError("EV charging simulator is unavailable")
        if name == "stop_ev":
            if value is not None:
                raise ValueError("stop_ev does not accept a value")
            self._power_kw = 0.0
            self._mode = "idle"
        elif name == "charge_ev":
            if value is None or isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError("EV charging command value must be finite")
            power = float(value)
            if power <= 0:
                raise ValueError("EV charging command value must be positive")
            if power > self.profile.max_charge_kw:
                raise ValueError("EV charging command exceeds max_charge_kw")
            if not self._connected:
                raise ValueError("EV charging command requires the vehicle to be connected")
            if self._soc_kwh >= self.profile.capacity_kwh:
                raise ValueError("EV is at maximum capacity")
            self._power_kw, self._mode = power, "charging"
        else:
            raise ValueError(f"unsupported EV charging command: {name}")
        self._idempotency_keys.add(idempotency_key)
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def tick(self, seconds: float | None = None) -> EVChargingSimulationState:
        duration = self.profile.tick_seconds if seconds is None else float(seconds)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("EV charging tick duration must be finite and non-negative")
        if self._fault == "unavailable":
            raise ConnectionError("EV charging simulator is unavailable")
        if self._mode == "charging":
            delta = self._power_kw * self.profile.charge_efficiency * duration / 3600
        else:
            delta = 0.0
        self._soc_kwh = min(self.profile.capacity_kwh, self._soc_kwh + delta)
        if self._soc_kwh >= self.profile.capacity_kwh:
            self._power_kw = 0.0
            self._mode = "idle"
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def set_connected(self, connected: bool) -> EVChargingSimulationState:
        self._connected = connected
        if not connected and self._mode == "charging":
            self._power_kw = 0.0
            self._mode = "idle"
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def set_departure_at(self, departure_at: datetime | None) -> EVChargingSimulationState:
        if departure_at is not None and departure_at.tzinfo is None:
            raise ValueError("EV charging departure_at must be timezone-aware")
        self._departure_at = departure_at
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()

    def set_fault(self, fault: EVChargingFault | None) -> EVChargingSimulationState:
        self._fault = fault
        self._revision += 1
        self._observed_at = self._clock.now()
        return self.snapshot()


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "EVChargingSimulationProfile",
    "EVChargingSimulationState",
    "EVChargingSimulator",
]
