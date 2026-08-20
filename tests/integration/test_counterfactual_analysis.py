from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.optimizer.counterfactual import CounterfactualAnalyzer
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.energy import EnergyContext, SolarForecastPoint, TariffPoint
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Constraint, Objective, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_horizon, flexible_load


async def _registry_and_device_id() -> tuple[DeviceRegistry, str]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    return registry, device_id


def _scenario(
    device_id: str,
    *,
    scenario_id: str,
    export_price_per_kwh: float,
    with_export_tariff: bool,
    max_grid_import: float = 10,
    solar_power_kw: float = 5.0,
) -> OptimizationScenario:
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    context = EnergyContext(
        horizon=horizon,
        tariffs=[
            TariffPoint(slot=0, price_per_kwh=0.10, currency="EUR"),
            TariffPoint(slot=1, price_per_kwh=0.10, currency="EUR"),
        ],
        solar_forecast=[
            SolarForecastPoint(slot=0, power=solar_power_kw),
            SolarForecastPoint(slot=1, power=solar_power_kw),
        ],
        export_tariffs=(
            [
                TariffPoint(slot=0, price_per_kwh=export_price_per_kwh, currency="EUR"),
                TariffPoint(slot=1, price_per_kwh=export_price_per_kwh, currency="EUR"),
            ]
            if with_export_tariff
            else None
        ),
        battery=None,
        source_revision="counterfactual-scenario",
        observed_at=datetime(2026, 8, 19, 12, tzinfo=UTC),
    )
    return OptimizationScenario(
        id=scenario_id,
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=1)],
        constraints=[
            Constraint(type="max_grid_import", value=max_grid_import, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )


@pytest.mark.asyncio
async def test_counterfactual_compares_feasible_variation_against_baseline() -> None:
    registry, device_id = await _registry_and_device_id()
    baseline = _scenario(
        device_id,
        scenario_id="cf-baseline-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
    )
    variation = _scenario(
        device_id,
        scenario_id="cf-variation-1",
        export_price_per_kwh=0.05,
        with_export_tariff=True,
    )
    analyzer = CounterfactualAnalyzer(CpSatOptimizer(registry))

    direct_baseline = CpSatOptimizer(registry).optimize(baseline)
    direct_variation = CpSatOptimizer(registry).optimize(variation)
    expected_diff = (
        direct_variation.objective_values["energy_cost"]
        - direct_baseline.objective_values["energy_cost"]
    )

    result = analyzer.compare(baseline, {"with_export_tariff": variation})

    assert result.baseline.status in {
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL_HIERARCHY,
    }
    outcome = result.variations["with_export_tariff"]
    assert outcome.result.status in {
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL_HIERARCHY,
    }
    assert outcome.diff["energy_cost"] == pytest.approx(expected_diff)


@pytest.mark.asyncio
async def test_counterfactual_compares_multiple_variations_independently() -> None:
    registry, device_id = await _registry_and_device_id()
    baseline = _scenario(
        device_id,
        scenario_id="cf-multi-baseline-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
    )
    cheap_export = _scenario(
        device_id,
        scenario_id="cf-multi-cheap-1",
        export_price_per_kwh=0.02,
        with_export_tariff=True,
    )
    expensive_export = _scenario(
        device_id,
        scenario_id="cf-multi-expensive-1",
        export_price_per_kwh=0.20,
        with_export_tariff=True,
    )
    analyzer = CounterfactualAnalyzer(CpSatOptimizer(registry))

    result = analyzer.compare(
        baseline, {"cheap_export": cheap_export, "expensive_export": expensive_export}
    )

    assert set(result.variations) == {"cheap_export", "expensive_export"}
    cheap_diff = result.variations["cheap_export"].diff["energy_cost"]
    expensive_diff = result.variations["expensive_export"].diff["energy_cost"]
    assert expensive_diff <= cheap_diff


@pytest.mark.asyncio
async def test_counterfactual_reports_infeasible_variation_without_fabricated_diff() -> None:
    registry, device_id = await _registry_and_device_id()
    baseline = _scenario(
        device_id,
        scenario_id="cf-infeasible-baseline-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
    )
    infeasible_variation = _scenario(
        device_id,
        scenario_id="cf-infeasible-variation-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
        max_grid_import=0,
        solar_power_kw=0.0,
    )
    analyzer = CounterfactualAnalyzer(CpSatOptimizer(registry))

    result = analyzer.compare(baseline, {"impossible": infeasible_variation})

    outcome = result.variations["impossible"]
    assert outcome.result.status is OptimizationStatus.INFEASIBLE
    assert outcome.diff == {}


@pytest.mark.asyncio
async def test_counterfactual_mixed_feasible_and_infeasible_variations() -> None:
    registry, device_id = await _registry_and_device_id()
    baseline = _scenario(
        device_id,
        scenario_id="cf-mixed-baseline-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
    )
    feasible_variation = _scenario(
        device_id,
        scenario_id="cf-mixed-feasible-1",
        export_price_per_kwh=0.05,
        with_export_tariff=True,
    )
    infeasible_variation = _scenario(
        device_id,
        scenario_id="cf-mixed-infeasible-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
        max_grid_import=0,
        solar_power_kw=0.0,
    )
    analyzer = CounterfactualAnalyzer(CpSatOptimizer(registry))

    result = analyzer.compare(
        baseline, {"feasible": feasible_variation, "infeasible": infeasible_variation}
    )

    assert result.variations["feasible"].diff != {}
    assert result.variations["infeasible"].diff == {}
    assert result.variations["infeasible"].result.status is OptimizationStatus.INFEASIBLE


@pytest.mark.asyncio
async def test_counterfactual_infeasible_baseline_skips_all_variations() -> None:
    registry, device_id = await _registry_and_device_id()
    infeasible_baseline = _scenario(
        device_id,
        scenario_id="cf-baseline-infeasible-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
        max_grid_import=0,
        solar_power_kw=0.0,
    )
    variation = _scenario(
        device_id,
        scenario_id="cf-baseline-infeasible-var-1",
        export_price_per_kwh=0.05,
        with_export_tariff=True,
    )
    analyzer = CounterfactualAnalyzer(CpSatOptimizer(registry))

    result = analyzer.compare(infeasible_baseline, {"anything": variation})

    assert result.baseline.status is OptimizationStatus.INFEASIBLE
    assert result.variations == {}


@pytest.mark.asyncio
async def test_counterfactual_zero_variations_returns_baseline_only() -> None:
    registry, device_id = await _registry_and_device_id()
    baseline = _scenario(
        device_id,
        scenario_id="cf-zero-variations-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
    )
    analyzer = CounterfactualAnalyzer(CpSatOptimizer(registry))

    result = analyzer.compare(baseline, {})

    assert result.variations == {}
    assert result.baseline.status in {
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL_HIERARCHY,
    }


@pytest.mark.asyncio
async def test_counterfactual_identical_variation_reports_zero_diff() -> None:
    registry, device_id = await _registry_and_device_id()
    baseline = _scenario(
        device_id,
        scenario_id="cf-identical-baseline-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
    )
    identical = baseline.model_copy(update={"id": "cf-identical-variation-1"})
    analyzer = CounterfactualAnalyzer(CpSatOptimizer(registry))

    result = analyzer.compare(baseline, {"identical": identical})

    for value in result.variations["identical"].diff.values():
        assert value == pytest.approx(0.0, abs=1e-9)


@pytest.mark.asyncio
async def test_counterfactual_never_mutates_supplied_scenarios() -> None:
    registry, device_id = await _registry_and_device_id()
    baseline = _scenario(
        device_id,
        scenario_id="cf-immutable-baseline-1",
        export_price_per_kwh=0.05,
        with_export_tariff=False,
    )
    variation = _scenario(
        device_id,
        scenario_id="cf-immutable-variation-1",
        export_price_per_kwh=0.05,
        with_export_tariff=True,
    )
    baseline_before = baseline.model_dump(mode="json")
    variation_before = variation.model_dump(mode="json")
    analyzer = CounterfactualAnalyzer(CpSatOptimizer(registry))

    analyzer.compare(baseline, {"variation": variation})

    assert baseline.model_dump(mode="json") == baseline_before
    assert variation.model_dump(mode="json") == variation_before
