"""Deterministic virtual physical plant used by the complete lab gate.

The plant is intentionally above the adapter layer: production adapters see
only a protocol projection, while the plant owns state transitions, virtual
time and repeatable fault injection.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from domoai.lab.battery_simulator import BatterySimulationProfile, BatterySimulator
from domoai.lab.ev_charging_simulator import EVChargingSimulationProfile, EVChargingSimulator
from domoai.lab.thermal_simulator import ThermalSimulationProfile, ThermalSimulator
from domoai.lab.water_consumption_simulator import (
    WaterConsumptionSimulationProfile,
    WaterConsumptionSimulator,
)
from domoai.runtime.clock import Clock

VirtualFault = Literal[
    "unavailable",
    "stale",
    "rejected",
    "delayed",
    "duplicate",
    "out_of_order",
    "partial_failure",
]


class VirtualPlantModel(Protocol):
    profile: Any

    def snapshot(self) -> Any: ...

    def tick(self, seconds: float | None = None) -> Any: ...


class VirtualPlantClock:
    """Mutable UTC clock whose advancement is explicit and deterministic."""

    def __init__(self, initial: datetime) -> None:
        if initial.tzinfo is None:
            raise ValueError("virtual plant clock requires a timezone-aware datetime")
        self._current = initial.astimezone(UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, duration: timedelta) -> None:
        if duration < timedelta(0):
            raise ValueError("virtual plant clock cannot move backwards")
        self._current += duration


@dataclass(frozen=True)
class VirtualPlantObservation:
    adapter_id: str
    source_id: str
    device_id: str
    domain: str
    capability: str
    value: bool | int | float | str | None
    available: bool
    observed_at: datetime
    revision: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "source_id": self.source_id,
            "device_id": self.device_id,
            "domain": self.domain,
            "capability": self.capability,
            "value": self.value,
            "available": self.available,
            "observed_at": self.observed_at.isoformat(),
            "revision": self.revision,
        }


@dataclass
class VirtualPlantDevice:
    adapter_id: str
    source_id: str
    device_id: str
    domain: str
    capabilities: tuple[str, ...]
    state: dict[str, bool | int | float | str | None]
    commands: dict[str, str] = field(default_factory=dict)
    bounds: dict[str, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.adapter_id.strip() or not self.source_id.strip() or not self.device_id.strip():
            raise ValueError("virtual plant device identity is required")
        if not self.capabilities or set(self.capabilities) != set(self.state):
            raise ValueError("virtual plant capabilities and state must match")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("virtual plant capabilities must be unique")


class VirtualHomePlant:
    """Shared virtual home with protocol-neutral command and observation APIs."""

    def __init__(
        self,
        devices: Iterable[VirtualPlantDevice],
        *,
        seed: int,
        clock: VirtualPlantClock | Clock | None = None,
    ) -> None:
        if seed <= 0:
            raise ValueError("virtual plant seed must be positive")
        materialized = [deepcopy(device) for device in devices]
        keys = [(device.adapter_id, device.source_id) for device in materialized]
        if len(keys) != len(set(keys)):
            raise ValueError("virtual plant source identities must be unique")
        canonical_ids = [device.device_id for device in materialized]
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("virtual plant canonical identities must be unique")
        self.seed = seed
        self.clock = clock or VirtualPlantClock(datetime(2026, 1, 1, tzinfo=UTC))
        self._devices = {(device.adapter_id, device.source_id): device for device in materialized}
        self._models: dict[str, VirtualPlantModel] = {}
        self._faults: dict[tuple[str, str], VirtualFault] = {}
        self._idempotency: dict[str, VirtualPlantObservation] = {}
        self._event_revisions: dict[tuple[str, str, str], int] = {}
        self._trace: list[dict[str, Any]] = []
        self._revision = 0
        self.write_count = 0
        self._initial_digest = self.digest()

    @classmethod
    def default(
        cls,
        *,
        seed: int = 187,
        clock: VirtualPlantClock | Clock | None = None,
    ) -> VirtualHomePlant:
        """Build the minimum complete home used by the deterministic matrix."""

        def device(
            adapter_id: str,
            source_id: str,
            device_id: str,
            domain: str,
            state: dict[str, bool | int | float | str | None],
            commands: dict[str, str],
            bounds: dict[str, tuple[float, float]] | None = None,
        ) -> VirtualPlantDevice:
            return VirtualPlantDevice(
                adapter_id=adapter_id,
                source_id=source_id,
                device_id=device_id,
                domain=domain,
                capabilities=tuple(state),
                state=state,
                commands=commands,
                bounds=bounds or {},
            )

        plant = cls(
            [
                device(
                    "fixture",
                    "fixture.light",
                    "fixture.light",
                    "light",
                    {"power": False, "brightness": 0},
                    {
                        "turn_on": "power",
                        "turn_off": "power",
                        "toggle": "power",
                        "set_brightness": "brightness",
                    },
                    {"brightness": (0, 100)},
                ),
                device(
                    "fixture",
                    "fixture.switch",
                    "fixture.switch",
                    "switch",
                    {"power": False},
                    {"turn_on": "power", "turn_off": "power", "toggle": "power"},
                ),
                device(
                    "fixture",
                    "fixture.cover",
                    "fixture.cover",
                    "cover",
                    {"position": 50},
                    {
                        "open": "position",
                        "close": "position",
                        "stop": "position",
                        "set_position": "position",
                    },
                    {"position": (0, 100)},
                ),
                device(
                    "fixture",
                    "fixture.climate",
                    "fixture.climate",
                    "climate",
                    {"temperature": 20.0, "target_temperature": 21.0},
                    {"set_temperature": "target_temperature"},
                    {"target_temperature": (16, 27)},
                ),
                device(
                    "fixture",
                    "fixture.environment",
                    "fixture.environment",
                    "environment",
                    {"temperature": 20.5, "humidity": 42.0, "occupancy": False},
                    {},
                ),
                device(
                    "fixture",
                    "fixture.power",
                    "fixture.power",
                    "power",
                    {"power": 420.0},
                    {},
                ),
                device(
                    "fixture",
                    "fixture.solar",
                    "fixture.solar",
                    "solar",
                    {"solar.power": 1200.0},
                    {},
                ),
                device(
                    "home_assistant",
                    "home_assistant.light",
                    "home_assistant.light",
                    "light",
                    {"power": False, "brightness": 0},
                    {
                        "turn_on": "power",
                        "turn_off": "power",
                        "toggle": "power",
                        "set_brightness": "brightness",
                    },
                    {"brightness": (0, 100)},
                ),
                device(
                    "knx",
                    "knx.light",
                    "knx.light",
                    "light",
                    {"power": False, "brightness": 0},
                    {
                        "turn_on": "power",
                        "turn_off": "power",
                        "toggle": "power",
                        "set_brightness": "brightness",
                    },
                    {"brightness": (0, 100)},
                ),
                device(
                    "modbus",
                    "modbus.battery",
                    "modbus.battery",
                    "battery",
                    {"battery.soc": 50.0, "battery.power": 0.0, "battery.capacity": 10.0},
                    {
                        "charge_battery": "battery.power",
                        "discharge_battery": "battery.power",
                        "stop_battery": "battery.power",
                    },
                    {"battery.soc": (0, 100)},
                ),
                device(
                    "modbus",
                    "modbus.ev",
                    "modbus.ev",
                    "ev",
                    {"ev.soc": 30.0, "ev_charging": 0.0, "ev.connected": True, "ev.capacity": 60.0},
                    {"charge_ev": "ev_charging", "stop_ev": "ev_charging"},
                    {"ev.soc": (0, 100)},
                ),
                device(
                    "modbus",
                    "modbus.water",
                    "modbus.water",
                    "water",
                    {"water.flow_rate": 0.0, "water.total_volume": 0.0},
                    {},
                    {"water.flow_rate": (0, math.inf), "water.total_volume": (0, math.inf)},
                ),
                device(
                    "modbus",
                    "modbus.thermal",
                    "modbus.thermal",
                    "thermal",
                    {
                        "thermal.indoor_temperature": 20.0,
                        "thermal.hvac_power": 0.0,
                        "thermal.hvac_mode": "off",
                    },
                    {"set_hvac_mode": "thermal.hvac_mode"},
                ),
                device(
                    "matter",
                    "node:1001/endpoint:1",
                    "matter.light",
                    "light",
                    {"power": False, "brightness": 0},
                    {
                        "turn_on": "power",
                        "turn_off": "power",
                        "toggle": "power",
                        "set_brightness": "brightness",
                    },
                    {"brightness": (0, 100)},
                ),
                device(
                    "zigbee2mqtt",
                    "living_room/main_light",
                    "zigbee2mqtt.light",
                    "light",
                    {"power": False, "brightness": 0},
                    {
                        "turn_on": "power",
                        "turn_off": "power",
                        "toggle": "power",
                        "set_brightness": "brightness",
                    },
                    {"brightness": (0, 100)},
                ),
            ],
            seed=seed,
            clock=clock,
        )
        plant._mount_default_models()
        plant._initial_digest = plant.digest()
        return plant

    @property
    def trace(self) -> list[dict[str, Any]]:
        return deepcopy(self._trace)

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def devices(self) -> tuple[VirtualPlantDevice, ...]:
        return tuple(deepcopy(device) for device in self._devices.values())

    @property
    def initial_digest(self) -> str:
        return self._initial_digest

    def mount(self, model: VirtualPlantModel) -> None:
        device_id = str(model.profile.device_id)
        if device_id in self._models:
            raise ValueError(f"virtual plant model already mounted: {device_id}")
        self._models[device_id] = model
        for device in self._devices.values():
            if device.device_id == device_id:
                self._sync_model_state(device)
        self._record("mount", device_id=device_id)

    def set_fault(self, adapter_id: str, source_id: str, fault: VirtualFault | None) -> None:
        self._device(adapter_id, source_id)
        key = (adapter_id, source_id)
        if fault is None:
            self._faults.pop(key, None)
        else:
            self._faults[key] = fault
        self._record("fault", adapter_id=adapter_id, source_id=source_id, fault=fault)

    def discover(self, adapter_id: str) -> list[dict[str, Any]]:
        devices = [device for device in self._devices.values() if device.adapter_id == adapter_id]
        self._record("discover", adapter_id=adapter_id, count=len(devices))
        return [self._entity(device) for device in sorted(devices, key=lambda item: item.source_id)]

    def read(self, adapter_id: str, source_id: str, capability: str) -> VirtualPlantObservation:
        device = self._device(adapter_id, source_id)
        self._sync_model_state(device)
        if capability not in device.state:
            raise ValueError(f"unsupported virtual plant capability: {capability}")
        fault = self._faults.get((adapter_id, source_id))
        available = fault != "unavailable"
        value = device.state[capability] if available else None
        observed_at = self.clock.now()
        if fault == "stale":
            observed_at -= timedelta(minutes=5)
        observation = VirtualPlantObservation(
            adapter_id=adapter_id,
            source_id=source_id,
            device_id=device.device_id,
            domain=device.domain,
            capability=capability,
            value=value,
            available=available,
            observed_at=observed_at,
            revision=self._revision,
        )
        self._record("read", **observation.as_dict())
        return observation

    def command(
        self,
        adapter_id: str,
        source_id: str,
        command: str,
        *,
        value: bool | int | float | str | None = None,
        idempotency_key: str,
    ) -> VirtualPlantObservation:
        if not idempotency_key.strip():
            raise ValueError("virtual plant idempotency_key is required")
        device = self._device(adapter_id, source_id)
        model = self._models.get(device.device_id)
        existing = self._idempotency.get(idempotency_key)
        if existing is not None:
            self._record(
                "command_duplicate", adapter_id=adapter_id, source_id=source_id, command=command
            )
            return existing
        fault = self._faults.get((adapter_id, source_id))
        if fault == "unavailable":
            self._record(
                "command_rejected", adapter_id=adapter_id, source_id=source_id, code="unavailable"
            )
            raise ConnectionError("virtual plant source is unavailable")
        if fault in {"rejected", "partial_failure"}:
            self._record("command_rejected", adapter_id=adapter_id, source_id=source_id, code=fault)
            raise ValueError("virtual plant command was rejected")
        if fault == "delayed":
            self._record(
                "command_delayed",
                adapter_id=adapter_id,
                source_id=source_id,
                command=command,
            )
        capability = device.commands.get(command)
        if capability is None:
            raise ValueError(f"unsupported virtual plant command: {command}")
        if model is not None and hasattr(model, "command"):
            model.command(
                command,
                value=None if command in {"stop_battery", "stop_ev"} else value,
                idempotency_key=idempotency_key,
            )
            self._sync_model_state(device)
            next_value = device.state[capability]
        elif model is not None and device.domain == "thermal" and command == "set_hvac_mode":
            if not isinstance(value, str):
                raise ValueError("thermal set_hvac_mode requires a mode")
            set_hvac_mode = getattr(model, "set_hvac_mode", None)
            if not callable(set_hvac_mode):
                raise ValueError("thermal model does not expose set_hvac_mode")
            set_hvac_mode(value)
            self._sync_model_state(device)
            next_value = device.state[capability]
        else:
            next_value = self._command_value(device, command, capability, value)
            device.state[capability] = next_value
        self._revision += 1
        self.write_count += 1
        observation = VirtualPlantObservation(
            adapter_id=adapter_id,
            source_id=source_id,
            device_id=device.device_id,
            domain=device.domain,
            capability=capability,
            value=next_value,
            available=True,
            observed_at=self.clock.now(),
            revision=self._revision,
        )
        self._idempotency[idempotency_key] = observation
        self._record(
            "command",
            adapter_id=adapter_id,
            source_id=source_id,
            command=command,
            capability=capability,
            value=next_value,
            revision=self._revision,
        )
        return observation

    def deliver_event(
        self,
        adapter_id: str,
        source_id: str,
        capability: str,
        value: bool | int | float | str | None,
        *,
        observed_at: datetime,
        revision: int,
    ) -> bool:
        """Apply one source event only when it is newer than the last one."""

        device = self._device(adapter_id, source_id)
        if capability not in device.state:
            raise ValueError(f"unsupported virtual plant capability: {capability}")
        if revision < 0:
            raise ValueError("virtual plant event revision must not be negative")
        key = (adapter_id, source_id, capability)
        previous = self._event_revisions.get(key, -1)
        if revision <= previous:
            self._record(
                "event_discarded",
                adapter_id=adapter_id,
                source_id=source_id,
                capability=capability,
                revision=revision,
                reason="duplicate" if revision == previous else "out_of_order",
            )
            return False
        device.state[capability] = value
        self._event_revisions[key] = revision
        self._revision = max(self._revision, revision)
        self._record(
            "event_applied",
            adapter_id=adapter_id,
            source_id=source_id,
            capability=capability,
            value=value,
            observed_at=observed_at.isoformat(),
            revision=revision,
        )
        return True

    def tick(self, duration: timedelta) -> None:
        if duration < timedelta(0):
            raise ValueError("virtual plant tick must not be negative")
        if isinstance(self.clock, VirtualPlantClock):
            self.clock.advance(duration)
        for model in self._models.values():
            model.tick(duration.total_seconds())
        for device in self._devices.values():
            self._sync_model_state(device)
        self._revision += 1
        self._record("tick", seconds=duration.total_seconds(), revision=self._revision)

    def model_snapshots(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for device_id, model in sorted(self._models.items()):
            snapshot = model.snapshot()
            as_dict = getattr(snapshot, "as_dict", None)
            result[device_id] = deepcopy(as_dict() if callable(as_dict) else snapshot.__dict__)
        return result

    def invariant_violations(self) -> list[str]:
        violations: list[str] = []
        for device in self._devices.values():
            for capability, value in device.state.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if not math.isfinite(float(value)):
                        violations.append(f"{device.device_id}.{capability} is not finite")
                bounds = device.bounds.get(capability)
                if (
                    bounds is not None
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and not bounds[0] <= float(value) <= bounds[1]
                ):
                    violations.append(
                        f"{device.device_id}.{capability} is outside configured bounds"
                    )
                if capability in {
                    "humidity",
                    "water.flow_rate",
                    "water.total_volume",
                    "solar.power",
                }:
                    if (
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and value < 0
                    ):
                        violations.append(f"{device.device_id}.{capability} is negative")
                if capability == "humidity" and isinstance(value, (int, float)):
                    if not 0 <= value <= 100:
                        violations.append(f"{device.device_id}.humidity is outside [0, 100]")
        for device_id, snapshot in self.model_snapshots().items():
            for field_name in ("soc_kwh", "indoor_temperature_c", "total_volume_l"):
                value = snapshot.get(field_name)
                if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                    violations.append(f"{device_id}.{field_name} is not finite")
            if "soc_kwh" in snapshot:
                profile = self._models[device_id].profile
                minimum = getattr(profile, "min_soc_kwh", 0.0)
                maximum = getattr(profile, "max_soc_kwh", profile.capacity_kwh)
                if not minimum <= snapshot["soc_kwh"] <= maximum:
                    violations.append(f"{device_id}.soc_kwh is outside configured bounds")
            if "capacity_kwh" in snapshot and "soc_kwh" in snapshot:
                if not 0 <= snapshot["soc_kwh"] <= snapshot["capacity_kwh"]:
                    violations.append(f"{device_id}.soc_kwh is outside capacity")
            if "total_volume_l" in snapshot and snapshot["total_volume_l"] < 0:
                violations.append(f"{device_id}.total_volume_l is negative")
        return violations

    def digest(self) -> str:
        payload = {
            "seed": self.seed,
            "devices": [
                self._entity(device)
                for device in sorted(
                    self._devices.values(), key=lambda item: (item.adapter_id, item.source_id)
                )
            ],
            "models": self.model_snapshots(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def trace_digest(self) -> str:
        canonical = json.dumps(self._trace, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _device(self, adapter_id: str, source_id: str) -> VirtualPlantDevice:
        try:
            return self._devices[(adapter_id, source_id)]
        except KeyError as error:
            raise KeyError(f"unknown virtual plant source: {adapter_id}/{source_id}") from error

    @staticmethod
    def _command_value(
        device: VirtualPlantDevice,
        command: str,
        capability: str,
        value: bool | int | float | str | None,
    ) -> bool | int | float | str | None:
        if command == "turn_on":
            return True
        if command == "turn_off":
            return False
        if command == "toggle":
            return not bool(device.state[capability])
        if command in {"open", "close"}:
            return 100 if command == "open" else 0
        if command in {"stop", "stop_battery", "stop_ev"}:
            return 0.0
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"virtual plant command {command} requires a scalar value")
        if isinstance(value, (int, float)) and capability in device.bounds:
            minimum, maximum = device.bounds[capability]
            if not minimum <= float(value) <= maximum:
                raise ValueError(f"virtual plant value outside {capability} bounds")
        return value

    def _entity(self, device: VirtualPlantDevice) -> dict[str, Any]:
        self._sync_model_state(device)
        return {
            "adapter_id": device.adapter_id,
            "source_id": device.source_id,
            "device_id": device.device_id,
            "domain": device.domain,
            "name": device.device_id,
            "capabilities": list(device.capabilities),
            "state": deepcopy(device.state),
            "available": self._faults.get((device.adapter_id, device.source_id)) != "unavailable",
        }

    def entity(self, device: VirtualPlantDevice) -> dict[str, Any]:
        """Return the public protocol-neutral entity projection."""

        return self._entity(device)

    def _mount_default_models(self) -> None:
        """Install the deterministic domain simulators used by the default plant."""

        clock = self.clock
        self.mount(
            BatterySimulator(
                BatterySimulationProfile(
                    provider_id="modbus",
                    device_id="modbus.battery",
                    capacity_kwh=10,
                    initial_soc_kwh=5,
                    min_soc_kwh=1,
                    max_soc_kwh=9,
                    max_charge_kw=4,
                    max_discharge_kw=4,
                    charge_efficiency=0.9,
                    discharge_efficiency=0.9,
                ),
                clock=clock,
            )
        )
        self.mount(
            EVChargingSimulator(
                EVChargingSimulationProfile(
                    provider_id="modbus",
                    device_id="modbus.ev",
                    capacity_kwh=60,
                    initial_soc_kwh=18,
                    max_charge_kw=7,
                    charge_efficiency=0.9,
                ),
                clock=clock,
            )
        )
        self.mount(
            ThermalSimulator(
                ThermalSimulationProfile(
                    provider_id="modbus",
                    device_id="modbus.thermal",
                    capacitance_kwh_per_c=0.5,
                    ua_kw_per_c=0.05,
                    initial_temperature_c=20,
                    initial_exterior_temperature_c=10,
                    max_heat_kw=2,
                    max_cool_kw=2,
                    heating_cop=3,
                    cooling_cop=2.5,
                ),
                clock=clock,
            )
        )
        self.mount(
            WaterConsumptionSimulator(
                WaterConsumptionSimulationProfile(
                    provider_id="modbus",
                    device_id="modbus.water",
                    initial_flow_rate_lpm=0,
                ),
                clock=clock,
            )
        )

    def _sync_model_state(self, device: VirtualPlantDevice) -> None:
        model = self._models.get(device.device_id)
        if model is None:
            return
        snapshot = model.snapshot()
        values = snapshot.as_dict() if hasattr(snapshot, "as_dict") else snapshot.__dict__
        if device.domain == "battery":
            device.state.update(
                {
                    "battery.soc": 100 * float(values["soc_kwh"]) / float(values["capacity_kwh"]),
                    "battery.power": float(values["power_kw"]),
                    "battery.capacity": float(values["capacity_kwh"]),
                }
            )
        elif device.domain == "ev":
            device.state.update(
                {
                    "ev.soc": 100 * float(values["soc_kwh"]) / float(values["capacity_kwh"]),
                    "ev_charging": float(values["power_kw"]),
                    "ev.connected": bool(values["connected"]),
                    "ev.capacity": float(values["capacity_kwh"]),
                }
            )
        elif device.domain == "thermal":
            device.state.update(
                {
                    "thermal.indoor_temperature": float(values["indoor_temperature_c"]),
                    "thermal.hvac_power": float(values["hvac_power_kw"]),
                    "thermal.hvac_mode": values["hvac_mode"],
                }
            )
        elif device.domain == "water":
            device.state.update(
                {
                    "water.flow_rate": float(values["flow_rate_lpm"]),
                    "water.total_volume": float(values["total_volume_l"]),
                }
            )

    def _record(self, operation: str, **payload: Any) -> None:
        entry = {"sequence": len(self._trace), "operation": operation, **payload}
        self._trace.append(deepcopy(entry))


__all__ = [
    "VirtualFault",
    "VirtualHomePlant",
    "VirtualPlantClock",
    "VirtualPlantDevice",
    "VirtualPlantObservation",
]
