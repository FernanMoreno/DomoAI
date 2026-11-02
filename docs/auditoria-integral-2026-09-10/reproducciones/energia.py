"""Read-only DomoAI energy audit. Run with project .venv/bin/python from repo root.

Findings: providers.py:476,569 + cp_sat.py:158; counterfactual.py:17,68,89;
cp_sat.py:565; energy.py:130 + cp_sat.py:513.
Assertions intentionally verify the observed faulty behavior, not desired behavior.
No hardware/network/database access or repository writes.
"""

from datetime import UTC, datetime

from domoai.optimizer.counterfactual import CounterfactualAnalyzer
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.energy import BatteryProfile, EnergyContext, SolarForecastPoint, TariffPoint
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.providers import (
    ComposedEnergyContextProvider,
    SolarForecastSeries,
    StaticSolarForecastProvider,
    StaticTariffProvider,
    TariffSeries,
)
from domoai.optimizer.scenario import Objective, OptimizationScenario
from domoai.runtime.registry import DeviceRegistry

h = Horizon(
    start=datetime(2026, 9, 10, tzinfo=UTC),
    end=datetime(2026, 9, 10, 2, tzinfo=UTC),
    resolution_minutes=60,
    timezone="Europe/Madrid",
)
m = dict(horizon=h, source_id="audit", source_revision="v1", observed_at=h.start)
p = ComposedEnergyContextProvider(
    StaticTariffProvider(
        TariffSeries(
            **m, points=[TariffPoint(slot=i, price_per_kwh=0.2, currency="EUR") for i in range(2)]
        )
    ),
    StaticSolarForecastProvider(
        SolarForecastSeries(**m, points=[SolarForecastPoint(slot=i, power=0) for i in range(2)])
    ),
    now=lambda: h.start,
)
c = p.get_context(h)
s = OptimizationScenario(
    id="audit",
    horizon=h,
    energy_context=c,
    objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
)
o = CpSatOptimizer(DeviceRegistry())


def context_with(**changes):
    return EnergyContext.model_validate(c.model_dump() | changes)


def solve(context):
    return o.optimize(s.model_copy(update={"energy_context": context}))


base = [{"slot": i, "power": 1} for i in range(2)]
c_base = context_with(base_load_forecast=base)
r_empty, r_base = solve(c), solve(c_base)
assert c.base_load_forecast is None
assert r_empty.objective_values["energy_cost"] == 0
assert r_base.objective_values["energy_cost"] == 0.4
print("CASE 1: composed base_load_forecast=None; cost=0.0; with 1kW base load cost=0.4")

baseline = s.model_copy(update={"energy_context": c_base})
comparison = CounterfactualAnalyzer(o).compare(baseline, {"alternative": baseline})
assert comparison.baseline.status.value == "no_action_required"
assert comparison.baseline.objective_values["energy_cost"] == 0.4
assert comparison.variations == {}
print("CASE 2: valid passive baseline cost=0.4; status=no_action_required; variations={}")

battery = BatteryProfile(
    capacity_kwh=1,
    initial_soc_kwh=1,
    min_soc_kwh=0,
    max_soc_kwh=1,
    max_charge_kw=1,
    max_discharge_kw=1,
    charge_efficiency=1,
    discharge_efficiency=1,
)
c_solar = context_with(
    battery=battery.model_dump(),
    solar_forecast=[{"slot": i, "power": 1 - i} for i in range(2)],
    base_load_forecast=[{"slot": i, "power": 1 - i} for i in range(2)],
    export_tariffs=[
        {"slot": 0, "price_per_kwh": -0.1, "currency": "EUR"},
        {"slot": 1, "price_per_kwh": 0.1, "currency": "EUR"},
    ],
)
r_solar = solve(c_solar)
slots = r_solar.constraint_summary["slots"]
assert slots[0]["solar_power_kw"] == 1
assert slots[0]["grid_import_kw"] == slots[0]["grid_export_kw"] == 0
assert slots[0]["battery_charge_kw"] == slots[0]["battery_discharge_kw"] == 0
assert slots[1]["solar_power_kw"] == 0
assert slots[1]["battery_discharge_kw"] == slots[1]["grid_export_kw"] == 1
assert r_solar.objective_values["solar_self_consumption_kwh"] == 0
print(
    "CASE 3: solar consumed locally=1kWh; later initial battery energy exported=1kWh; "
    "reported solar self-consumption=0kWh"
)

c_mixed = context_with(
    base_load_forecast=base,
    tariffs=[
        {"slot": 0, "price_per_kwh": 0.2, "currency": "EUR"},
        {"slot": 1, "price_per_kwh": 0.2, "currency": "USD"},
    ],
)
r_mixed = solve(c_mixed)
assert not r_mixed.diagnostics
assert r_mixed.objective_values["energy_cost"] == 0.4
print("CASE 4: 0.2EUR + 0.2USD accepted without conversion/diagnostic; energy_cost=0.4")
print("All four reported behaviors reproduced. No repository files changed.")
