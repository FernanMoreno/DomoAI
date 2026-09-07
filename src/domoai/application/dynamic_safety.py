"""JIT physical guards for actuator commands whose safety depends on state."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from domoai.domain.energy import EVChargingBinding
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
        battery_profile: BatteryProfile | None = None,
        *,
        ev_bindings: list[EVChargingBinding] | tuple[EVChargingBinding, ...] = (),
        clock: Clock | None = None,
    ) -> None:
        if battery_profile is None and not ev_bindings:
            raise ValueError("dynamic safety requires a battery profile or EV binding")
        if battery_profile is not None and battery_profile.actuator is None:
            raise ValueError("dynamic battery safety requires an actuator binding")
        self.state_store = state_store
        self.profile = battery_profile
        self.actuator = battery_profile.actuator if battery_profile is not None else None
        self.ev_bindings = {binding.device_id: binding for binding in ev_bindings}
        self.clock = clock or state_store.clock or SystemClock()

    async def check(self, command: Command) -> ErrorDetail | None:
        if self.actuator is not None and command.device_id == self.actuator.device_id:
            return await self._check_battery(command)
        ev_binding = self.ev_bindings.get(command.device_id)
        if ev_binding is not None:
            return await self._check_ev(command, ev_binding)
        return None

    async def _check_battery(self, command: Command) -> ErrorDetail | None:
        assert self.actuator is not None
        assert self.profile is not None
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
        if (error := self._require_current(snapshot, "Battery SOC evidence")) is not None:
            return error
        assert snapshot is not None
        if not isinstance(snapshot.value, (int, float)) or isinstance(snapshot.value, bool):
            return self._blocked("Battery SOC evidence is not numeric")
        power_snapshot = await self.state_store.get(
            command.device_id, self.actuator.power_feedback_capability
        )
        if (
            error := self._require_current(power_snapshot, "Battery power evidence")
        ) is not None:
            return error
        assert power_snapshot is not None
        if not isinstance(power_snapshot.value, (int, float)) or isinstance(
            power_snapshot.value, bool
        ):
            return self._blocked("Battery power evidence is not numeric")
        if not math.isfinite(float(snapshot.value)) or not math.isfinite(
            float(power_snapshot.value)
        ):
            return self._blocked("Battery physical evidence must be finite")
        value = float(command.value)
        soc = float(snapshot.value)
        current_power = abs(float(power_snapshot.value))
        power_limit = (
            self.profile.max_charge_kw
            if command.command == self.actuator.charge_command
            else self.profile.max_discharge_kw
        )
        if value < 0 or value > power_limit or current_power > max(
            self.profile.max_charge_kw, self.profile.max_discharge_kw
        ):
            return self._blocked("Battery command exceeds the bound actuator power envelope")
        if command.command == self.actuator.charge_command and soc >= self.profile.max_soc_kwh:
            return self._blocked("Battery command would exceed the current SOC maximum")
        if command.command == self.actuator.discharge_command and soc <= self.profile.min_soc_kwh:
            return self._blocked("Battery command would cross the current SOC reserve")
        return None

    async def _check_ev(
        self, command: Command, binding: EVChargingBinding
    ) -> ErrorDetail | None:
        if command.command == binding.stop_command:
            return None
        if command.command != binding.charge_command:
            return None
        if not isinstance(command.value, (int, float)) or isinstance(command.value, bool):
            return self._blocked(
                "EV charging value must be numeric",
                device_id=binding.device_id,
                capability=binding.capability,
            )
        value = float(command.value)
        if not math.isfinite(value) or value < 0 or value > binding.max_charge_kw:
            return self._blocked(
                "EV command exceeds the bound charging power envelope",
                device_id=binding.device_id,
                capability=binding.capability,
            )
        states: dict[str, StateSnapshot | None] = {
            name: await self.state_store.get(binding.device_id, name)
            for name in (
                binding.connected_capability,
                binding.soc_capability,
                binding.power_feedback_capability,
                binding.departure_capability,
            )
        }
        for name, snapshot in states.items():
            if (
                error := self._require_current(
                    snapshot,
                    f"EV evidence {name!r}",
                    device_id=binding.device_id,
                    capability=name,
                )
            ) is not None:
                return error
        connected = states[binding.connected_capability]
        soc = states[binding.soc_capability]
        power = states[binding.power_feedback_capability]
        departure = states[binding.departure_capability]
        assert connected is not None
        assert soc is not None
        assert power is not None
        assert departure is not None
        if connected.value is not True:
            return self._blocked(
                "EV is not currently connected",
                device_id=binding.device_id,
                capability=binding.connected_capability,
            )
        if (
            not isinstance(soc.value, (int, float))
            or isinstance(soc.value, bool)
            or not math.isfinite(float(soc.value))
            or float(soc.value) < 0
            or float(soc.value) >= binding.capacity_kwh
        ):
            return self._blocked(
                "EV SOC evidence is invalid or already full",
                device_id=binding.device_id,
                capability=binding.soc_capability,
            )
        if (
            not isinstance(power.value, (int, float))
            or isinstance(power.value, bool)
            or not math.isfinite(float(power.value))
            or abs(float(power.value)) > binding.max_charge_kw
        ):
            return self._blocked(
                "EV power evidence exceeds the charging envelope",
                device_id=binding.device_id,
                capability=binding.power_feedback_capability,
            )
        if not isinstance(departure.value, str):
            return self._blocked(
                "EV departure evidence is missing",
                device_id=binding.device_id,
                capability=binding.departure_capability,
            )
        try:
            departure_at = datetime.fromisoformat(departure.value.replace("Z", "+00:00"))
        except ValueError:
            return self._blocked(
                "EV departure evidence is invalid",
                device_id=binding.device_id,
                capability=binding.departure_capability,
            )
        if departure_at.tzinfo is None or departure_at.astimezone(UTC) <= self.clock.now():
            return self._blocked(
                "EV departure has already elapsed",
                device_id=binding.device_id,
                capability=binding.departure_capability,
            )
        return None

    def _require_current(
        self,
        snapshot: StateSnapshot | None,
        evidence_name: str,
        *,
        device_id: str | None = None,
        capability: str | None = None,
    ) -> ErrorDetail | None:
        if snapshot is None:
            return self._blocked(
                f"{evidence_name} is missing", device_id=device_id, capability=capability
            )
        if snapshot.status is not StateStatus.CURRENT:
            return self._blocked(
                f"{evidence_name} is not current", device_id=device_id, capability=capability
            )
        age = self.clock.now() - snapshot.observed_at
        if age > self.state_store.stale_after:
            return self._blocked(
                f"{evidence_name} has expired", device_id=device_id, capability=capability
            )
        if age.total_seconds() < 0:
            return self._blocked(
                f"{evidence_name} has a future observation timestamp",
                device_id=device_id,
                capability=capability,
            )
        return None

    def _blocked(
        self,
        message: str,
        *,
        device_id: str | None = None,
        capability: str | None = None,
    ) -> ErrorDetail:
        return ErrorDetail(
            code=ErrorCode.SAFETY_LIMIT_EXCEEDED,
            message=message,
            device_id=device_id or (self.actuator.device_id if self.actuator else None),
            capability=capability
            or (self.actuator.soc_reconciliation_capability if self.actuator else None),
            retryable=True,
            details={"authority": "dynamic_safety_guard"},
        )


__all__ = ["DynamicSafetyGuard"]
