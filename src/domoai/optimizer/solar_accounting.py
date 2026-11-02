"""Horizon-local modeled attribution; no measured origin or persistent inventory.

CP-SAT owns the flows. This module validates their integer conservation and
converts their exact rational energy to bounded, typed public evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel

Energy = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Origin = Literal["horizon_solar", "horizon_grid", "initial_unknown"]
ORIGINS: tuple[Origin, ...] = ("horizon_solar", "horizon_grid", "initial_unknown")


def _equal(left: float, right: float) -> None:
    if not math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-9):
        raise ValueError("Accounting energy does not conserve")


class SolarAccountingConvention(StrictModel):
    accounting_version: Literal["direct_solar_first_destination_conservative_v1"] = (
        "direct_solar_first_destination_conservative_v1"
    )
    evidence_kind: Literal["modeled_attribution"] = "modeled_attribution"
    horizon_scope: Literal["current_horizon_only"] = "current_horizon_only"
    unit: Literal["kWh"] = "kWh"
    initial_origin: Literal["unknown"] = "unknown"
    objective_basis: Literal["native_power_floor"] = "native_power_floor"
    power_scale: int = Field(gt=0)
    soc_scale: int = Field(gt=0)
    efficiency_scale: int = Field(gt=0)
    resolution_minutes: int = Field(gt=0)
    charge_gain: int = Field(ge=0)
    discharge_cost: int = Field(gt=0)
    quantization_bound_kwh: Energy

    @model_validator(mode="after")
    def validate_units(self) -> SolarAccountingConvention:
        if self.power_scale != self.soc_scale:
            raise ValueError("Attribution requires equal power and SOC scales")
        if self.charge_gain > self.efficiency_scale or self.discharge_cost < self.efficiency_scale:
            raise ValueError("Rounded efficiencies must not create energy")
        return self


class BatteryOriginEvidence(StrictModel):
    origin: Origin
    initial_kwh: Energy
    charged_kwh: Energy
    local_delivered_kwh: Energy
    exported_kwh: Energy
    charge_loss_kwh: Energy
    discharge_loss_kwh: Energy
    terminal_stored_kwh: Energy

    @model_validator(mode="after")
    def validate_balance(self) -> BatteryOriginEvidence:
        if self.origin == "initial_unknown" and self.charged_kwh != 0:
            raise ValueError("Horizon charging cannot have initial-unknown origin")
        _equal(
            self.initial_kwh + self.charged_kwh,
            self.local_delivered_kwh + self.exported_kwh + self.charge_loss_kwh
            + self.discharge_loss_kwh + self.terminal_stored_kwh,
        )
        return self


class SolarAccountingEvidence(StrictModel):
    scope: Literal["horizon", "slot"]
    convention: SolarAccountingConvention
    generated_kwh: Energy
    direct_local_kwh: Energy
    battery_local_kwh: Energy
    self_consumption_kwh: Energy
    exported_kwh: Energy
    charge_loss_kwh: Energy
    discharge_loss_kwh: Energy
    terminal_stored_kwh: Energy
    objective_credit_kwh: Energy
    local_quantization_remainder_kwh: Energy
    battery_origins: list[BatteryOriginEvidence] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_attribution(self) -> SolarAccountingEvidence:
        if tuple(item.origin for item in self.battery_origins) != ORIGINS:
            raise ValueError("Exactly one ordered record per battery origin is required")
        solar, grid, _initial = self.battery_origins
        if self.scope == "horizon" and (solar.initial_kwh != 0 or grid.initial_kwh != 0):
            raise ValueError("Initial battery energy has unknown horizon origin")
        _equal(self.battery_local_kwh, solar.local_delivered_kwh)
        _equal(self.charge_loss_kwh, solar.charge_loss_kwh)
        _equal(self.discharge_loss_kwh, solar.discharge_loss_kwh)
        _equal(self.terminal_stored_kwh, solar.terminal_stored_kwh)
        _equal(self.self_consumption_kwh, self.direct_local_kwh + self.battery_local_kwh)
        _equal(
            self.self_consumption_kwh,
            self.objective_credit_kwh + self.local_quantization_remainder_kwh,
        )
        _equal(
            self.generated_kwh + solar.initial_kwh,
            self.self_consumption_kwh + self.exported_kwh + self.charge_loss_kwh
            + self.discharge_loss_kwh + self.terminal_stored_kwh,
        )
        if self.exported_kwh < solar.exported_kwh:
            raise ValueError("Solar exports cannot exclude battery solar exports")
        if self.local_quantization_remainder_kwh > self.convention.quantization_bound_kwh:
            raise ValueError("Local remainder exceeds the native-power quantum bound")
        return self


@dataclass(frozen=True)
class SolarAccountingFlow:
    """One solved slot, in native power and scaled internal-energy numerators."""

    generated: int
    direct_local: int
    direct_export: int
    credit_power: int
    initial: tuple[int, ...]
    terminal: tuple[int, ...]
    charged: tuple[int, ...]
    local_draw: tuple[int, ...]
    export_draw: tuple[int, ...]


def accounting_evidence(
    flows: list[SolarAccountingFlow],
    convention: SolarAccountingConvention,
    *,
    scope: Literal["horizon", "slot"],
) -> SolarAccountingEvidence:
    """Conserve exactly before a single float conversion for public serialization."""
    if not flows or (scope == "slot" and len(flows) != 1):
        raise ValueError("Accounting requires a nonempty, contiguous scope")
    p, e = convention.power_scale, convention.efficiency_scale
    m, g, d = convention.resolution_minutes, convention.charge_gain, convention.discharge_cost
    power_energy = Fraction(m, 60 * p)
    internal_energy = Fraction(1, 60 * p * e)
    delivered_energy = Fraction(1, 60 * p * d)
    for index, flow in enumerate(flows):
        vectors = (flow.initial, flow.terminal, flow.charged, flow.local_draw, flow.export_draw)
        if any(len(vector) != 3 or min(vector) < 0 for vector in vectors):
            raise ValueError("Three nonnegative battery origins are required")
        if index and flows[index - 1].terminal != flow.initial:
            raise ValueError("Accounting slots have discontinuous inventory")
        if flow.generated != flow.direct_local + flow.direct_export + flow.charged[0]:
            raise ValueError("Solar bus allocation does not conserve")
        if flow.credit_power != flow.direct_local + flow.local_draw[0] // (m * d):
            raise ValueError("Credit differs from native-power floor")
        for origin_index in range(3):
            if (
                flow.initial[origin_index] + m * g * flow.charged[origin_index]
                != flow.terminal[origin_index] + flow.local_draw[origin_index]
                + flow.export_draw[origin_index]
            ):
                raise ValueError("Integer battery-origin balance does not conserve")
    origins = []
    for index, origin in enumerate(ORIGINS):
        charged_power = sum(flow.charged[index] for flow in flows)
        local_draw = sum(flow.local_draw[index] for flow in flows)
        export_draw = sum(flow.export_draw[index] for flow in flows)
        initial = flows[0].initial[index] * internal_energy
        charged = charged_power * power_energy
        local = local_draw * delivered_energy
        exported = export_draw * delivered_energy
        charge_loss = charged_power * m * (e - g) * internal_energy
        discharge_loss = (local_draw + export_draw) * (internal_energy - delivered_energy)
        terminal = flows[-1].terminal[index] * internal_energy
        if initial + charged != local + exported + charge_loss + discharge_loss + terminal:
            raise ValueError("Rational battery-origin balance does not conserve")
        origins.append(BatteryOriginEvidence(
            origin=origin, initial_kwh=float(initial), charged_kwh=float(charged),
            local_delivered_kwh=float(local), exported_kwh=float(exported),
            charge_loss_kwh=float(charge_loss), discharge_loss_kwh=float(discharge_loss),
            terminal_stored_kwh=float(terminal),
        ))
    generated = sum(flow.generated for flow in flows) * power_energy
    direct = sum(flow.direct_local for flow in flows) * power_energy
    battery_local = sum(flow.local_draw[0] for flow in flows) * delivered_energy
    self_consumption = direct + battery_local
    exported = (
        sum(flow.direct_export for flow in flows) * power_energy
        + sum(flow.export_draw[0] for flow in flows) * delivered_energy
    )
    credit = sum(flow.credit_power for flow in flows) * power_energy
    remainder = self_consumption - credit
    bound = len(flows) * power_energy
    if not 0 <= remainder < bound:
        raise ValueError("Rational local quantum bound violated")
    scoped_convention = convention.model_copy(update={"quantization_bound_kwh": float(bound)})
    return SolarAccountingEvidence(
        scope=scope, convention=scoped_convention, generated_kwh=float(generated),
        direct_local_kwh=float(direct), battery_local_kwh=float(battery_local),
        self_consumption_kwh=float(self_consumption), exported_kwh=float(exported),
        charge_loss_kwh=origins[0].charge_loss_kwh,
        discharge_loss_kwh=origins[0].discharge_loss_kwh,
        terminal_stored_kwh=origins[0].terminal_stored_kwh,
        objective_credit_kwh=float(credit), local_quantization_remainder_kwh=float(remainder),
        battery_origins=origins,
    )
