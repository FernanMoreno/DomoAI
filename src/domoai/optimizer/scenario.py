"""Solver-neutral optimization scenario models and semantic validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field, model_validator

from domoai.domain.models import CapabilityKind, ErrorDetail, ScalarValue, StrictModel
from domoai.optimizer.energy import BatteryActuator, EnergyContext, HVACActuator
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
    "TerminalSOCPolicy",
    "validate_scenario",
    "validate_executable_scenario",
]

MAX_HORIZON_SLOTS = 7 * 24 * 60


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
    end_command: str | None = Field(default=None, min_length=1)
    end_value: ScalarValue | None = None


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
    # Keep parsing permissive so external payloads receive the normal
    # structured scenario diagnostic instead of a raw model error.
    end_command: str | None = Field(default=None, min_length=1)
    end_value: ScalarValue | None = None

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


class TerminalSOCPolicy(StrictModel):
    """Explicit rolling-horizon reserve/terminal-value policy."""

    minimum_kwh: float | None = Field(default=None, ge=0)
    target_kwh: float | None = Field(default=None, ge=0)
    value_eur_per_kwh: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_policy(self) -> TerminalSOCPolicy:
        if self.minimum_kwh is None and self.target_kwh is None and self.value_eur_per_kwh is None:
            raise ValueError("terminal SOC policy must declare a minimum, target or value")
        if (
            self.minimum_kwh is not None
            and self.target_kwh is not None
            and self.target_kwh < self.minimum_kwh
        ):
            raise ValueError("terminal SOC target must not be below terminal minimum")
        return self


class OptimizationScenario(StrictModel):
    schema_version: str = "v1"
    id: str = Field(min_length=1)
    horizon: Horizon
    # Bounds on every agent-facing list field below (spec 148): the MCP
    # boundary parses this scenario from an untrusted caller before the
    # solve ever reaches the bounded OptimizationWorker (P2.1) -- Pydantic
    # parsing, validate_scenario, and registry lookups all run inline on
    # the event loop first, so an unbounded scenario is itself a resource
    # exhaustion vector independent of solver time limits.
    loads: list[Load] = Field(default_factory=list, max_length=100)
    ev_loads: list[EVChargingLoad] = Field(default_factory=list, max_length=16)
    comfort_loads: list[ComfortLoad] = Field(default_factory=list, max_length=32)
    constraints: list[Constraint] = Field(default_factory=list, max_length=64)
    objectives: list[Objective] = Field(default_factory=list, max_length=16)
    energy_context: EnergyContext | None = None
    terminal_soc_policy: TerminalSOCPolicy | None = None
    inputs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
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
    scenario: OptimizationScenario,
    registry: DeviceRegistry,
    *,
    max_horizon_slots: int = MAX_HORIZON_SLOTS,
) -> list[ErrorDetail]:
    errors: list[ErrorDetail] = []
    horizon_slots = scenario.horizon.slots
    if horizon_slots < 1:
        return [_diagnostic("invalid_horizon", "Horizon does not contain a complete slot")]
    if max_horizon_slots <= 0:
        raise ValueError("max_horizon_slots must be positive")
    if horizon_slots > max_horizon_slots:
        errors.append(
            _diagnostic(
                "horizon_too_large",
                f"Horizon contains {horizon_slots} slots; maximum is {max_horizon_slots}",
            )
        )
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
        if load.duration_slots > 1 and load.end_command is None:
            errors.append(
                _diagnostic(
                    "missing_deactivation_command",
                    f"Load {load.id!r} spans multiple slots but does not specify end_command",
                )
            )
        if (
            load.end_command is not None
            and capability is not None
            and capability.writable
            and load.end_command not in capability.commands
        ):
            errors.append(
                _diagnostic(
                    "unsupported_command",
                    f"Command {load.end_command!r} is not supported by {load.capability!r}",
                )
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
    if scenario.energy_context is not None:
        battery = scenario.energy_context.battery
        if battery is not None and battery.actuator is not None:
            errors.extend(_validate_battery_actuator(registry, battery.actuator))
        thermal = scenario.energy_context.thermal
        if thermal is not None and thermal.actuator is not None:
            errors.extend(_validate_hvac_actuator(registry, thermal.actuator))
    if scenario.terminal_soc_policy is not None:
        battery = scenario.energy_context.battery if scenario.energy_context else None
        if battery is None:
            errors.append(
                _diagnostic(
                    "terminal_soc_requires_battery",
                    "Terminal SOC policy requires a battery energy context",
                )
            )
        else:
            policy = scenario.terminal_soc_policy
            for name, value in (
                ("minimum_kwh", policy.minimum_kwh),
                ("target_kwh", policy.target_kwh),
            ):
                if value is not None and value > battery.max_soc_kwh:
                    errors.append(
                        _diagnostic(
                            "terminal_soc_out_of_range",
                            f"Terminal SOC {name} exceeds battery maximum",
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
        if comfort_load.end_command is None:
            errors.append(
                _diagnostic(
                    "missing_deactivation_command",
                    f"Comfort load {comfort_load.id!r} must specify end_command",
                )
            )
        else:
            errors.extend(
                _validate_device_capability_command(
                    registry,
                    comfort_load.device_id,
                    comfort_load.capability,
                    comfort_load.end_command,
                    comfort_load.unit,
                )
            )
        if comfort_load.power_unit not in {"W", "kW"}:
            errors.append(
                _diagnostic("invalid_unit", f"Unsupported power unit {comfort_load.power_unit!r}")
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
            "comfort_temp_min",
            "comfort_temp_max",
        }:
            errors.append(
                _diagnostic("unsupported_constraint", f"Unsupported constraint {constraint.type!r}")
            )
        if constraint.type.startswith("battery_"):
            allowed_units = {"kWh"}
        elif constraint.type.startswith("comfort_temp_"):
            allowed_units = {"degC"}
        else:
            allowed_units = {"W", "kW"}
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
        if constraint.type.startswith("comfort_temp_") and (
            scenario.energy_context is None or scenario.energy_context.thermal is None
        ):
            errors.append(
                _diagnostic(
                    "missing_thermal_profile",
                    f"Constraint {constraint.type!r} requires a thermal profile",
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


def validate_executable_scenario(
    scenario: OptimizationScenario,
    registry: DeviceRegistry,
    *,
    max_horizon_slots: int = MAX_HORIZON_SLOTS,
) -> list[ErrorDetail]:
    """Add provider-evidence gates required before physical proposal validation."""

    errors = validate_scenario(
        scenario, registry, max_horizon_slots=max_horizon_slots
    )
    if not scenario.ev_loads:
        return errors
    context = scenario.energy_context
    states = {state.device_id: state for state in context.ev_states} if context else {}
    for ev_load in scenario.ev_loads:
        state = states.get(ev_load.device_id)
        if state is None:
            errors.append(
                _diagnostic(
                    "ev_state_provenance_missing",
                    f"EV {ev_load.device_id!r} has no provider-observed state",
                )
            )
            continue
        if state.quality.value != "good":
            errors.append(
                _diagnostic(
                    "ev_state_unavailable",
                    f"EV {ev_load.device_id!r} state quality is not good",
                )
            )
        if context is not None and state.source_revision != context.source_revision:
            errors.append(
                _diagnostic(
                    "ev_state_conflict",
                    f"EV {ev_load.device_id!r} state revision does not match energy context",
                )
            )
        if not state.connected:
            errors.append(
                _diagnostic(
                    "ev_disconnected",
                    f"EV {ev_load.device_id!r} is not connected",
                )
            )
        if abs(ev_load.initial_soc_kwh - state.soc_kwh) > 1e-6:
            errors.append(
                _diagnostic(
                    "ev_state_conflict",
                    f"EV {ev_load.device_id!r} caller SOC differs from observed state",
                )
            )
        if ev_load.capacity_kwh != state.capacity_kwh:
            errors.append(
                _diagnostic(
                    "ev_state_conflict",
                    f"EV {ev_load.device_id!r} caller capacity differs from observed state",
                )
            )
        if ev_load.max_charge_kw > state.max_charge_kw:
            errors.append(
                _diagnostic(
                    "ev_state_conflict",
                    f"EV {ev_load.device_id!r} requested charge limit exceeds observed limit",
                )
            )
        if state.departure_at is not None:
            departure_at = state.departure_at.astimezone(UTC)
            if departure_at <= datetime.now(UTC):
                errors.append(
                    _diagnostic(
                        "ev_departure_elapsed",
                        f"EV {ev_load.device_id!r} departure has already elapsed",
                    )
                )
            deadline_at = scenario.horizon.start.astimezone(UTC) + timedelta(
                minutes=ev_load.deadline_slot * scenario.horizon.resolution_minutes
            )
            if deadline_at > departure_at:
                errors.append(
                    _diagnostic(
                        "ev_departure_after_deadline",
                        f"EV {ev_load.device_id!r} deadline is later than observed departure",
                    )
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
    capability = next((item for item in device.capabilities if item.name == capability_name), None)
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


def _validate_battery_actuator(
    registry: DeviceRegistry, actuator: BatteryActuator
) -> list[ErrorDetail]:
    device = registry.get(actuator.device_id)
    if device is None:
        return [_diagnostic("missing_device", f"Unknown device {actuator.device_id}")]
    capability = next(
        (item for item in device.capabilities if item.name == actuator.capability),
        None,
    )
    if capability is None or not capability.writable:
        return [
            _diagnostic(
                "missing_capability",
                f"Writable capability {actuator.capability!r} is not available on "
                f"{actuator.device_id}",
            )
        ]

    errors: list[ErrorDetail] = []
    feedback_capability = next(
        (item for item in device.capabilities if item.name == actuator.power_feedback_capability),
        None,
    )
    if feedback_capability is None or not feedback_capability.readable:
        errors.append(
            _diagnostic(
                "missing_feedback_capability",
                f"Readable battery feedback capability {actuator.power_feedback_capability!r} "
                f"is not available on {actuator.device_id}",
            )
        )
    else:
        if feedback_capability.kind is not CapabilityKind.NUMBER:
            errors.append(
                _diagnostic(
                    "invalid_feedback_capability",
                    "Battery power feedback capability must be numeric",
                )
            )
        if feedback_capability.unit != actuator.power_unit:
            errors.append(
                _diagnostic(
                    "feedback_unit_mismatch",
                    f"Battery power feedback must use {actuator.power_unit}",
                )
            )
        feedback_routes = registry.routes_for(
            actuator.device_id, actuator.power_feedback_capability
        )
        available_feedback_routes = [route for route in feedback_routes if route.available]
        if len(available_feedback_routes) > 1:
            errors.append(
                _diagnostic(
                    "feedback_route_ambiguous",
                    "Battery power feedback must have exactly one available route",
                )
            )
        elif not available_feedback_routes:
            errors.append(
                _diagnostic(
                    "feedback_route_unavailable",
                    "No available route exists for battery power feedback",
                )
            )
    if actuator.soc_reconciliation_capability is not None:
        soc_capability = next(
            (
                item
                for item in device.capabilities
                if item.name == actuator.soc_reconciliation_capability
            ),
            None,
        )
        if soc_capability is None or not soc_capability.readable:
            errors.append(
                _diagnostic(
                    "missing_soc_reconciliation_capability",
                    "Readable SOC reconciliation capability is not available",
                )
            )
        else:
            soc_routes = registry.routes_for(
                actuator.device_id, actuator.soc_reconciliation_capability
            )
            available_soc_routes = [route for route in soc_routes if route.available]
            if len(available_soc_routes) > 1:
                errors.append(
                    _diagnostic(
                        "soc_reconciliation_route_ambiguous",
                        "SOC reconciliation must have exactly one available route",
                    )
                )
            elif not available_soc_routes:
                errors.append(
                    _diagnostic(
                        "soc_reconciliation_route_unavailable",
                        "No available route exists for SOC reconciliation",
                    )
                )
    for command, unit in (
        (actuator.charge_command, actuator.power_unit),
        (actuator.discharge_command, actuator.power_unit),
        (actuator.stop_command, None),
    ):
        errors.extend(
            _validate_device_capability_command(
                registry, actuator.device_id, actuator.capability, command, unit
            )
        )
        if command in capability.commands:
            route = registry.resolve_command_route(actuator.device_id, command)
            if route.route is None:
                errors.append(
                    _diagnostic(
                        "route_unavailable",
                        f"No executable route is available for battery command {command!r}",
                    )
                )
    return errors


def _validate_hvac_actuator(
    registry: DeviceRegistry, actuator: HVACActuator
) -> list[ErrorDetail]:
    device = registry.get(actuator.device_id)
    if device is None:
        return [_diagnostic("missing_device", f"Unknown device {actuator.device_id}")]
    capability = next(
        (item for item in device.capabilities if item.name == actuator.capability),
        None,
    )
    if capability is None or not capability.writable:
        return [
            _diagnostic(
                "missing_capability",
                f"Writable capability {actuator.capability!r} is not available on "
                f"{actuator.device_id}",
            )
        ]

    errors: list[ErrorDetail] = []
    feedback_capability = next(
        (item for item in device.capabilities if item.name == actuator.power_feedback_capability),
        None,
    )
    if feedback_capability is None or not feedback_capability.readable:
        errors.append(
            _diagnostic(
                "missing_feedback_capability",
                f"Readable HVAC feedback capability {actuator.power_feedback_capability!r} "
                f"is not available on {actuator.device_id}",
            )
        )
    else:
        if feedback_capability.kind is not CapabilityKind.NUMBER:
            errors.append(
                _diagnostic(
                    "invalid_feedback_capability",
                    "HVAC power feedback capability must be numeric",
                )
            )
        if feedback_capability.unit != actuator.power_unit:
            errors.append(
                _diagnostic(
                    "feedback_unit_mismatch",
                    f"HVAC power feedback must use {actuator.power_unit}",
                )
            )
        feedback_routes = registry.routes_for(
            actuator.device_id, actuator.power_feedback_capability
        )
        available_feedback_routes = [route for route in feedback_routes if route.available]
        if len(available_feedback_routes) > 1:
            errors.append(
                _diagnostic(
                    "feedback_route_ambiguous",
                    "HVAC power feedback must have exactly one available route",
                )
            )
        elif not available_feedback_routes:
            errors.append(
                _diagnostic(
                    "feedback_route_unavailable",
                    "No available route exists for HVAC power feedback",
                )
            )
    for command, unit in (
        (actuator.heat_command, actuator.power_unit),
        (actuator.cool_command, actuator.power_unit),
        (actuator.stop_command, None),
    ):
        errors.extend(
            _validate_device_capability_command(
                registry, actuator.device_id, actuator.capability, command, unit
            )
        )
        if command in capability.commands:
            route = registry.resolve_command_route(actuator.device_id, command)
            if route.route is None:
                errors.append(
                    _diagnostic(
                        "route_unavailable",
                        f"No executable route is available for HVAC command {command!r}",
                    )
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
