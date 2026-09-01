"""JIT physical guards for actuator commands whose safety depends on state."""

from __future__ import annotations

from datetime import datetime, timedelta

from domoai.domain.energy import EVActuator
from domoai.domain.errors import ErrorCode
from domoai.domain.models import Command, ErrorDetail, StateSnapshot, StateStatus
from domoai.optimizer.energy import BatteryProfile
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.state_store import StateStore


class DynamicSafetyGuard:
    """Recheck battery reserve and envelope immediately before a write."""

    def __init__(
        self,
        state_store: StateStore,
        battery_profile: BatteryProfile | None,
        *,
        ev_actuators: tuple[EVActuator, ...] = (),
        clock: Clock | None = None,
    ) -> None:
        if battery_profile is not None and battery_profile.actuator is None:
            raise ValueError("dynamic battery safety requires an actuator binding")
        if battery_profile is None and not ev_actuators:
            raise ValueError("dynamic safety requires a battery profile or EV actuator binding")
        self.state_store = state_store
        self.profile = battery_profile
        self.actuator = battery_profile.actuator if battery_profile is not None else None
        self.ev_actuators = ev_actuators
        self.clock = clock or state_store.clock or SystemClock()

    async def check(self, command: Command) -> ErrorDetail | None:
        if self.actuator is not None:
            error = await self._check_battery(command)
            if error is not None:
                return error
        for actuator in self.ev_actuators:
            if command.device_id != actuator.device_id:
                continue
            return await self._check_ev(command, actuator)
        return None

    async def _check_battery(self, command: Command) -> ErrorDetail | None:
        assert self.actuator is not None
        assert self.profile is not None
        if command.device_id != self.actuator.device_id:
            return None
        if command.command not in {
            self.actuator.charge_command,
            self.actuator.discharge_command,
        }:
            return None
        if not isinstance(command.value, (int, float)) or isinstance(command.value, bool):
            return ErrorDetail(
                code=ErrorCode.SAFETY_LIMIT_EXCEEDED,
                message="Battery dispatch value must be numeric",
                device_id=command.device_id,
                capability=self.actuator.capability,
                retryable=False,
            )
        soc_capability = self.actuator.soc_reconciliation_capability
        if soc_capability is None:
            return self._blocked("Battery dispatch requires a bound SOC readback capability")
        snapshot = await self.state_store.get(command.device_id, soc_capability)
        if snapshot is None:
            return self._blocked("Battery SOC evidence is not current")
        snapshot = self.state_store.effective_snapshot(snapshot, self.clock.now())
        if snapshot.status is StateStatus.STALE:
            return self._blocked("Battery SOC evidence has expired")
        if snapshot.status is not StateStatus.CURRENT:
            return self._blocked("Battery SOC evidence is not current")
        age = self.clock.now() - snapshot.received_at
        if age < timedelta(0):
            return self._blocked("Battery SOC evidence is not current")
        if age > self.state_store.stale_after:
            return self._blocked("Battery SOC evidence has expired")
        if not isinstance(snapshot.value, (int, float)) or isinstance(snapshot.value, bool):
            return self._blocked("Battery SOC evidence is not numeric")
        value = float(command.value)
        soc = float(snapshot.value)
        power_limit = (
            self.profile.max_charge_kw
            if command.command == self.actuator.charge_command
            else self.profile.max_discharge_kw
        )
        if value < 0 or value > power_limit:
            return self._blocked("Battery command exceeds the bound actuator power envelope")
        if command.command == self.actuator.charge_command and soc >= self.profile.max_soc_kwh:
            return self._blocked("Battery command would exceed the current SOC maximum")
        if command.command == self.actuator.discharge_command and soc <= self.profile.min_soc_kwh:
            return self._blocked("Battery command would cross the current SOC reserve")
        return None

    async def _check_ev(self, command: Command, actuator: EVActuator) -> ErrorDetail | None:
        # A stop is the fail-safe operation and must remain available even if
        # the charger has already gone offline or its telemetry is stale.
        if command.command == actuator.stop_command:
            return None
        if command.command != actuator.charge_command:
            return None
        if not isinstance(command.value, (int, float)) or isinstance(command.value, bool):
            return self._ev_blocked(actuator, "EV charge value must be numeric")
        value = float(command.value)
        if value <= 0 or value > actuator.max_charge_kw:
            return self._ev_blocked(
                actuator, "EV command exceeds the bound actuator power envelope"
            )

        connected = await self.state_store.get(command.device_id, actuator.connected_capability)
        if connected is None or not self._is_fresh(connected) or connected.value is not True:
            return self._ev_blocked(actuator, "EV connected evidence is not current and true")

        if actuator.departure_capability is None:
            return None
        departure = await self.state_store.get(command.device_id, actuator.departure_capability)
        if not self._is_fresh(departure):
            return self._ev_blocked(actuator, "EV departure evidence is not current")
        if departure is None or departure.value is None:
            return None
        if not isinstance(departure.value, str):
            return self._ev_blocked(actuator, "EV departure evidence is not a timestamp")
        try:
            departure_at = datetime.fromisoformat(departure.value.replace("Z", "+00:00"))
        except ValueError:
            return self._ev_blocked(actuator, "EV departure evidence is not a valid timestamp")
        if departure_at.tzinfo is None or self.clock.now() >= departure_at:
            return self._ev_blocked(actuator, "EV departure time has elapsed")
        return None

    def _is_fresh(self, snapshot: StateSnapshot | None) -> bool:
        if snapshot is None:
            return False
        now = self.clock.now()
        snapshot = self.state_store.effective_snapshot(snapshot, now)
        if snapshot.status is not StateStatus.CURRENT:
            return False
        age = now - snapshot.received_at
        return timedelta(0) <= age <= self.state_store.stale_after

    def _ev_blocked(self, actuator: EVActuator, message: str) -> ErrorDetail:
        return ErrorDetail(
            code=ErrorCode.SAFETY_LIMIT_EXCEEDED,
            message=message,
            device_id=actuator.device_id,
            capability=actuator.capability,
            retryable=True,
            details={"authority": "dynamic_safety_guard", "actuator": "ev"},
        )

    def _blocked(self, message: str) -> ErrorDetail:
        assert self.actuator is not None
        return ErrorDetail(
            code=ErrorCode.SAFETY_LIMIT_EXCEEDED,
            message=message,
            device_id=self.actuator.device_id,
            capability=self.actuator.soc_reconciliation_capability,
            retryable=True,
            details={"authority": "dynamic_safety_guard"},
        )


__all__ = ["DynamicSafetyGuard"]
