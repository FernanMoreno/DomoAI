"""Solver-neutral optimization scenario models and semantic validation."""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from domoai.domain.models import ErrorDetail, ScalarValue, StrictModel
from domoai.optimizer.energy import EnergyContext
from domoai.optimizer.horizon import Horizon
from domoai.runtime.registry import DeviceRegistry

__all__ = [
    "ComfortLoad",
    "Constraint",
    "EVChargingLoad",
    "Horizon",
    "Load",
    "Objective",
    "OptimizationScenario",
    "validate_scenario",
]


class Load(StrictModel):
    id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    command: str = Field(min_length=1)
    value: ScalarValue | None = None
    unit: str | None = None
    power: float = Field(default=0, ge=0)
    power_unit: str = Field(default="W", min_length=1)
    duration_slots: int = Field(default=1, gt=0)
    earliest_slot: int = Field(default=0, ge=0)
    latest_slot: int | None = Field(default=None, ge=0)
    energy_required_kwh: float | None = Field(default=None, ge=0)
    deadline_slot: int | None = Field(default=None, ge=0)


class EVChargingLoad(StrictModel):
    id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    command: str = Field(min_length=1)
    unit: str | None = None
    capacity_kwh: float = Field(gt=0)
    initial_soc_kwh: float = Field(ge=0)
    target_soc_kwh: float = Field(gt=0)
    max_charge_kw: float = Field(gt=0)
    min_charge_kw: float = Field(default=0, ge=0)
    deadline_slot: int = Field(ge=0)
    charge_efficiency: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_state_domain(self) -> EVChargingLoad:
        if self.initial_soc_kwh > self.capacity_kwh:
            raise ValueError("initial_soc_kwh must be less than or equal to capacity_kwh")
        if self.target_soc_kwh > self.capacity_kwh:
            raise ValueError("target_soc_kwh must be less than or equal to capacity_kwh")
        if self.min_charge_kw > self.max_charge_kw:
            raise ValueError("min_charge_kw must be less than or equal to max_charge_kw")
        return self


class ComfortLoad(StrictModel):
    id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    command: str = Field(min_length=1)
    value: ScalarValue | None = None
    unit: str | None = None
    power: float = Field(default=0, ge=0)
    power_unit: str = Field(default="W", min_length=1)
    earliest_slot: int = Field(ge=0)
    deadline_slot: int = Field(ge=0)
    min_active_slots: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_window(self) -> ComfortLoad:
        if self.deadline_slot <= self.earliest_slot:
            raise ValueError("deadline_slot must be greater than earliest_slot")
        if self.min_active_slots > self.deadline_slot - self.earliest_slot:
            raise ValueError("min_active_slots cannot exceed the window size")
        return self


class Constraint(StrictModel):
    type: str = Field(min_length=1)
    value: float = Field(ge=0)
    unit: str = Field(default="W", min_length=1)
    hard: bool = True


class Objective(StrictModel):
    name: str = Field(min_length=1)
    direction: str = Field(pattern=r"^(minimize|maximize)$")
    weight: float = Field(default=1, gt=0)
    priority: int = 0


class OptimizationScenario(StrictModel):
    schema_version: str = "v1"
    id: str = Field(min_length=1)
    horizon: Horizon
    loads: list[Load] = Field(default_factory=list)
    ev_loads: list[EVChargingLoad] = Field(default_factory=list)
    comfort_loads: list[ComfortLoad] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    objectives: list[Objective] = Field(default_factory=list)
    energy_context: EnergyContext | None = None
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: dict[str, Any] = Field(default_factory=dict)
    solver_time_limit_seconds: float = Field(default=5.0, ge=0)
    conservative: bool = False

    @model_validator(mode="after")
    def validate_loads(self) -> OptimizationScenario:
        load_ids = (
            [load.id for load in self.loads]
            + [load.id for load in self.ev_loads]
            + [load.id for load in self.comfort_loads]
        )
        if len(load_ids) != len(set(load_ids)):
            raise ValueError("load ids must be unique")
        return self


def validate_scenario(
    scenario: OptimizationScenario, registry: DeviceRegistry
) -> list[ErrorDetail]:
    errors: list[ErrorDetail] = []
    horizon_slots = scenario.horizon.slots
    if horizon_slots < 1:
        return [_diagnostic("invalid_horizon", "Horizon does not contain a complete slot")]
    if scenario.energy_context is not None and scenario.energy_context.horizon != scenario.horizon:
        errors.append(
            _diagnostic(
                "context_horizon_mismatch",
                "Energy context horizon does not match the optimization scenario",
            )
        )
    if _contains_non_semantic_input(scenario.inputs) or _contains_non_semantic_input(
        scenario.assumptions
    ):
        errors.append(
            _diagnostic(
                "non_semantic_input",
                "Scenario inputs cannot contain vendor, adapter, protocol or executable "
                "solver fields",
            )
        )
    for load in scenario.loads:
        device = registry.get(load.device_id)
        if device is None:
            errors.append(_diagnostic("missing_device", f"Unknown device {load.device_id}"))
            continue
        capability = next(
            (item for item in device.capabilities if item.name == load.capability),
            None,
        )
        if capability is None or not capability.writable:
            errors.append(
                _diagnostic(
                    "missing_capability",
                    f"Writable capability {load.capability!r} is not available on {load.device_id}",
                )
            )
        elif load.command not in capability.commands:
            errors.append(
                _diagnostic(
                    "unsupported_command",
                    f"Command {load.command!r} is not supported by {load.capability!r}",
                )
            )
        if load.unit is not None and capability is not None and load.unit != capability.unit:
            errors.append(
                _diagnostic(
                    "invalid_unit",
                    f"Command unit {load.unit!r} does not match {capability.unit!r}",
                )
            )
        if load.power_unit not in {"W", "kW"}:
            errors.append(
                _diagnostic("invalid_unit", f"Unsupported power unit {load.power_unit!r}")
            )
        if load.energy_required_kwh is not None and load.power > 0:
            slot_hours = scenario.horizon.resolution_minutes / 60
            derived = _power_in_kw(load.power, load.power_unit) * slot_hours * load.duration_slots
            if abs(derived - load.energy_required_kwh) > 0.00001:
                errors.append(
                    _diagnostic(
                        "inconsistent_energy",
                        f"Load {load.id!r} energy_required_kwh does not match power and duration",
                    )
                )
        if load.deadline_slot is not None and load.deadline_slot >= horizon_slots:
            errors.append(
                _diagnostic(
                    "invalid_horizon",
                    f"Load {load.id!r} deadline_slot is outside the scenario horizon",
                )
            )
        upper = load.latest_slot
        if upper is None:
            upper = horizon_slots - load.duration_slots
        if load.deadline_slot is not None:
            upper = min(upper, load.deadline_slot - load.duration_slots + 1)
        if load.earliest_slot > upper or upper + load.duration_slots > horizon_slots:
            errors.append(
                _diagnostic(
                    "invalid_horizon",
                    f"Load {load.id!r} cannot fit within the scenario horizon",
                )
            )
    if (scenario.ev_loads or scenario.comfort_loads) and scenario.energy_context is None:
        errors.append(
            _diagnostic(
                "energy_context_required",
                "EV charging and comfort loads require an energy context",
            )
        )
    if scenario.conservative and scenario.energy_context is not None:
        if not all(
            point.confidence is not None for point in scenario.energy_context.solar_forecast
        ):
            errors.append(
                _diagnostic(
                    "conservative_mode_requires_confidence",
                    "Conservative mode requires confidence bounds on every solar_forecast slot",
                )
            )
        base_load_forecast = scenario.energy_context.base_load_forecast
        if base_load_forecast is not None and not all(
            point.confidence is not None for point in base_load_forecast
        ):
            errors.append(
                _diagnostic(
                    "conservative_mode_requires_confidence",
                    "Conservative mode requires confidence bounds on every base_load_forecast slot",
                )
            )
    for ev_load in scenario.ev_loads:
        errors.extend(
            _validate_device_capability_command(
                registry, ev_load.device_id, ev_load.capability, ev_load.command, ev_load.unit
            )
        )
        if ev_load.deadline_slot >= horizon_slots:
            errors.append(
                _diagnostic(
                    "invalid_horizon",
                    f"EV load {ev_load.id!r} deadline_slot is outside the scenario horizon",
                )
            )
    for comfort_load in scenario.comfort_loads:
        errors.extend(
            _validate_device_capability_command(
                registry,
                comfort_load.device_id,
                comfort_load.capability,
                comfort_load.command,
                comfort_load.unit,
            )
        )
        if comfort_load.power_unit not in {"W", "kW"}:
            errors.append(
                _diagnostic(
                    "invalid_unit", f"Unsupported power unit {comfort_load.power_unit!r}"
                )
            )
        if comfort_load.deadline_slot > horizon_slots:
            errors.append(
                _diagnostic(
                    "invalid_horizon",
                    f"Comfort load {comfort_load.id!r} window is outside the scenario horizon",
                )
            )
    for constraint in scenario.constraints:
        if constraint.type not in {
            "max_house_power",
            "max_grid_import",
            "max_grid_export",
            "battery_min_soc",
            "battery_max_soc",
        }:
            errors.append(
                _diagnostic("unsupported_constraint", f"Unsupported constraint {constraint.type!r}")
            )
        allowed_units = {"kWh"} if constraint.type.startswith("battery_") else {"W", "kW"}
        if constraint.unit not in allowed_units:
            errors.append(
                _diagnostic("invalid_unit", f"Unsupported constraint unit {constraint.unit!r}")
            )
        if scenario.energy_context is None and constraint.type != "max_house_power":
            errors.append(
                _diagnostic(
                    "energy_context_required",
                    f"Constraint {constraint.type!r} requires an energy context",
                )
            )
        if constraint.type.startswith("battery_") and (
            scenario.energy_context is None or scenario.energy_context.battery is None
        ):
            errors.append(
                _diagnostic(
                    "missing_battery",
                    f"Constraint {constraint.type!r} requires a battery profile",
                )
            )
    for objective in scenario.objectives:
        if objective.name not in {
            "minimize_start",
            "minimize_energy_cost",
            "minimize_peak_import",
            "maximize_solar_self_consumption",
        }:
            errors.append(
                _diagnostic("unsupported_objective", f"Unsupported objective {objective.name!r}")
            )
    return errors


def _validate_device_capability_command(
    registry: DeviceRegistry,
    device_id: str,
    capability_name: str,
    command: str,
    unit: str | None,
) -> list[ErrorDetail]:
    errors: list[ErrorDetail] = []
    device = registry.get(device_id)
    if device is None:
        errors.append(_diagnostic("missing_device", f"Unknown device {device_id}"))
        return errors
    capability = next(
        (item for item in device.capabilities if item.name == capability_name), None
    )
    if capability is None or not capability.writable:
        errors.append(
            _diagnostic(
                "missing_capability",
                f"Writable capability {capability_name!r} is not available on {device_id}",
            )
        )
        return errors
    if command not in capability.commands:
        errors.append(
            _diagnostic(
                "unsupported_command",
                f"Command {command!r} is not supported by {capability_name!r}",
            )
        )
    if unit is not None and unit != capability.unit:
        errors.append(
            _diagnostic("invalid_unit", f"Command unit {unit!r} does not match {capability.unit!r}")
        )
    return errors


def _power_in_kw(value: float, unit: str) -> float:
    if unit == "W":
        return value / 1000
    return value


def _contains_non_semantic_input(value: Any) -> bool:
    forbidden_keys = {
        "adapter",
        "adapter_id",
        "code",
        "manufacturer",
        "protocol",
        "python",
        "solver_code",
        "vendor",
        "vendor_name",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden_keys or _contains_non_semantic_input(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_non_semantic_input(item) for item in value)
    return False


def _diagnostic(code: str, message: str) -> ErrorDetail:
    return ErrorDetail(code=code, message=message)
