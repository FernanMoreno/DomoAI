
from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.domain.models import PlanStatus
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.energy import BaseLoadPoint, EnergyContext, SolarForecastPoint, TariffPoint
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Constraint, Horizon, Objective, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for, energy_horizon, flexible_load


async def build_context() -> tuple[SimulatedHomeAdapter, DeviceRegistry, OptimizationService]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    return adapter, registry, OptimizationService(registry, plan_service, CpSatOptimizer(registry))


@pytest.mark.asyncio
async def test_energy_optimizer_returns_balanced_proposal_with_storage_evidence() -> None:
    adapter, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context = energy_context_for()
    scenario = OptimizationScenario(
        id="energy-feasible-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.5, latest_slot=5)],
        constraints=[
            Constraint(type="max_house_power", value=3, unit="kW"),
            Constraint(type="max_grid_import", value=3, unit="kW"),
            Constraint(type="max_grid_export", value=3, unit="kW"),
        ],
        objectives=[
            Objective(name="minimize_energy_cost", direction="minimize"),
            Objective(name="maximize_solar_self_consumption", direction="maximize"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.plan is not None
    assert result.plan.status is PlanStatus.DRAFT
    assert adapter.calls == []
    assert result.constraint_summary["hard_satisfied"] is True
    assert len(result.constraint_summary["slots"]) == context.horizon.slots
    assert result.objective_values["energy_cost"] >= 0
    assert all(
        abs(
            slot["load_power_kw"] + slot["battery_charge_kw"]
            - slot["solar_power_kw"]
            - slot["grid_import_kw"]
            - slot["battery_discharge_kw"]
            + slot["grid_export_kw"]
        )
        < 0.00001
        for slot in result.constraint_summary["slots"]
    )


@pytest.mark.asyncio
async def test_energy_optimizer_rejects_impossible_grid_limit_without_plan() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="energy-infeasible-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=2, earliest_slot=0, latest_slot=0)],
        constraints=[Constraint(type="max_grid_import", value=0, unit="kW")],
    )

    result = service.optimize(scenario)

    assert result.status is OptimizationStatus.INFEASIBLE
    assert result.plan is None
    assert result.diagnostics


@pytest.mark.asyncio
async def test_maximize_solar_self_consumption_shifts_load_into_solar_slot() -> None:
    """Golden scenario: only one mathematically optimal answer exists."""

    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    context = EnergyContext(
        horizon=horizon,
        tariffs=[
            TariffPoint(slot=0, price_per_kwh=0.10, currency="EUR"),
            TariffPoint(slot=1, price_per_kwh=0.10, currency="EUR"),
        ],
        solar_forecast=[
            SolarForecastPoint(slot=0, power=0.0),
            SolarForecastPoint(slot=1, power=5.0),
        ],
        battery=None,
        source_revision="golden-solar-direction",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    scenario = OptimizationScenario(
        id="solar-direction-1",
        horizon=horizon,
        energy_context=context,
        loads=[
            flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=1, duration_slots=1)
        ],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[Objective(name="maximize_solar_self_consumption", direction="maximize")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.plan is not None
    command = next(cmd for cmd in result.plan.commands if cmd.device_id == device_id)
    assert command.intent == "scheduled_slot:1"


def _two_load_priority_scenario(horizon, device_a: str, device_b: str):
    context = EnergyContext(
        horizon=horizon,
        tariffs=[
            TariffPoint(slot=0, price_per_kwh=0.05, currency="EUR"),
            TariffPoint(slot=1, price_per_kwh=0.30, currency="EUR"),
        ],
        solar_forecast=[
            SolarForecastPoint(slot=0, power=0.0),
            SolarForecastPoint(slot=1, power=0.0),
        ],
        battery=None,
        source_revision="priority-scenario",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    loads = [
        flexible_load(device_a, load_id="load-a", power_kw=1.0, earliest_slot=0, latest_slot=1),
        flexible_load(device_b, load_id="load-b", power_kw=2.0, earliest_slot=0, latest_slot=1),
    ]
    return context, loads


@pytest.mark.asyncio
async def test_higher_priority_objective_is_never_traded_away() -> None:
    _, registry, service = await build_context()
    switches = [device.id for device in registry.devices if device.type.value == "switch"]
    device_a, device_b = switches[0], switches[0]
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    context, loads = _two_load_priority_scenario(horizon, device_a, device_b)
    scenario = OptimizationScenario(
        id="priority-lexicographic-1",
        horizon=horizon,
        energy_context=context,
        loads=loads,
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[
            Objective(name="minimize_peak_import", direction="minimize", priority=0),
            Objective(name="minimize_energy_cost", direction="minimize", priority=1),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.objective_values["peak_import_kw"] == pytest.approx(2.0)
    assert result.objective_values["energy_cost"] == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_same_priority_objectives_remain_a_single_weighted_tier() -> None:
    _, registry, service = await build_context()
    switches = [device.id for device in registry.devices if device.type.value == "switch"]
    device_a, device_b = switches[0], switches[0]
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    context, loads = _two_load_priority_scenario(horizon, device_a, device_b)
    scenario = OptimizationScenario(
        id="priority-blended-1",
        horizon=horizon,
        energy_context=context,
        loads=loads,
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[
            Objective(name="minimize_energy_cost", direction="minimize", weight=100, priority=0),
            Objective(name="minimize_peak_import", direction="minimize", weight=1, priority=0),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    # A heavily cost-weighted blend within the SAME tier sacrifices peak
    # (both loads land in the cheap slot) instead of respecting a strict
    # peak-first priority order.
    assert result.objective_values["peak_import_kw"] == pytest.approx(3.0)


def _single_slot_scenario(*, base_load_kw: float | None) -> tuple[EnergyContext, Horizon]:
    horizon = Horizon(
        start=datetime(2026, 8, 15, tzinfo=UTC),
        end=datetime(2026, 8, 15, 0, 15, tzinfo=UTC),
        resolution_minutes=15,
        timezone="Europe/Madrid",
    )
    context = EnergyContext(
        horizon=horizon,
        tariffs=[TariffPoint(slot=0, price_per_kwh=0.10, currency="EUR")],
        solar_forecast=[SolarForecastPoint(slot=0, power=0.0)],
        battery=None,
        base_load_forecast=(
            [BaseLoadPoint(slot=0, power=base_load_kw)] if base_load_kw is not None else None
        ),
        source_revision="base-load-scenario",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    return context, horizon


@pytest.mark.asyncio
async def test_soft_constraint_violation_is_reported_when_exceeded() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _single_slot_scenario(base_load_kw=None)
    scenario = OptimizationScenario(
        id="soft-constraint-violated-1",
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=2.0, earliest_slot=0, latest_slot=0)],
        constraints=[Constraint(type="max_house_power", value=1, unit="kW", hard=False)],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    violations = result.constraint_summary["soft_violations"]
    assert len(violations) == 1
    assert violations[0]["type"] == "max_house_power"
    assert violations[0]["slot"] == 0
    assert violations[0]["amount"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_soft_constraint_respected_when_free_reports_no_violation() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _single_slot_scenario(base_load_kw=None)
    scenario = OptimizationScenario(
        id="soft-constraint-respected-1",
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=2.0, earliest_slot=0, latest_slot=0)],
        constraints=[Constraint(type="max_house_power", value=5, unit="kW", hard=False)],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.constraint_summary["soft_violations"] == []


@pytest.mark.asyncio
async def test_base_load_forecast_is_reflected_in_import_and_cost() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _single_slot_scenario(base_load_kw=1.0)
    scenario = OptimizationScenario(
        id="base-load-present-1",
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=0)],
        constraints=[Constraint(type="max_grid_import", value=10, unit="kW")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.constraint_summary["slots"][0]["grid_import_kw"] == pytest.approx(2.0)
    assert result.objective_values["energy_cost"] == pytest.approx(0.05)


def _three_tier_scenario(device_a: str, device_b: str) -> tuple[OptimizationScenario, Horizon]:
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    context, loads = _two_load_priority_scenario(horizon, device_a, device_b)
    scenario = OptimizationScenario(
        id="three-tier-evidence-1",
        horizon=horizon,
        energy_context=context,
        loads=loads,
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
            Constraint(type="max_house_power", value=0.5, unit="kW", hard=False),
        ],
        objectives=[
            Objective(name="minimize_peak_import", direction="minimize", priority=0),
            Objective(name="minimize_energy_cost", direction="minimize", priority=1),
        ],
    )
    return scenario, horizon


@pytest.mark.asyncio
async def test_reproducibility_two_independent_solves_produce_identical_evidence() -> None:
    _, registry_a, service_a = await build_context()
    _, registry_b, service_b = await build_context()
    switches_a = [device.id for device in registry_a.devices if device.type.value == "switch"]
    switches_b = [device.id for device in registry_b.devices if device.type.value == "switch"]
    scenario_a, _ = _three_tier_scenario(switches_a[0], switches_a[0])
    scenario_b, _ = _three_tier_scenario(switches_b[0], switches_b[0])

    result_a = service_a.optimize(scenario_a)
    result_b = service_b.optimize(scenario_b)

    assert result_a.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result_a.plan is not None
    assert result_b.plan is not None
    # Plan.created_at is a wall-clock timestamp, not part of the
    # reproducibility claim (same reasoning as excluding wall_time_seconds).
    assert result_a.plan.model_dump(exclude={"created_at"}) == result_b.plan.model_dump(
        exclude={"created_at"}
    )
    assert result_a.objective_values == result_b.objective_values
    assert result_a.solver_evidence is not None
    assert result_b.solver_evidence is not None
    assert result_a.solver_evidence.tiers == result_b.solver_evidence.tiers
    assert (
        result_a.solver_evidence.scenario_fingerprint
        == result_b.solver_evidence.scenario_fingerprint
    )
    # wall_time_seconds is intentionally excluded: it may legitimately vary
    # between runs and is not part of the reproducibility claim.


@pytest.mark.asyncio
async def test_fingerprint_changes_when_a_scenario_field_changes() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _single_slot_scenario(base_load_kw=None)
    scenario_a = OptimizationScenario(
        id="fingerprint-a",
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=0)],
        constraints=[Constraint(type="max_grid_import", value=10, unit="kW")],
    )
    changed_context = context.model_copy(
        update={
            "tariffs": [TariffPoint(slot=0, price_per_kwh=0.99, currency="EUR")],
        }
    )
    scenario_b = scenario_a.model_copy(
        update={"id": "fingerprint-b", "energy_context": changed_context}
    )

    result_a = service.optimize(scenario_a)
    result_b = service.optimize(scenario_b)

    assert result_a.solver_evidence is not None
    assert result_b.solver_evidence is not None
    assert (
        result_a.solver_evidence.scenario_fingerprint
        != result_b.solver_evidence.scenario_fingerprint
    )


@pytest.mark.asyncio
async def test_evidence_absent_for_infeasible_result() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="evidence-absent-infeasible-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=2, earliest_slot=0, latest_slot=0)],
        constraints=[Constraint(type="max_grid_import", value=0, unit="kW")],
    )

    result = service.optimize(scenario)

    assert result.status is OptimizationStatus.INFEASIBLE
    assert result.solver_evidence is None


@pytest.mark.asyncio
async def test_tier_evidence_reports_soft_tier_then_priorities_in_order() -> None:
    _, registry, service = await build_context()
    switches = [device.id for device in registry.devices if device.type.value == "switch"]
    scenario, _ = _three_tier_scenario(switches[0], switches[0])

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.solver_evidence is not None
    tiers = result.solver_evidence.tiers
    assert len(tiers) == 3
    assert tiers[0].priority is None
    assert tiers[0].terms == ["max_house_power"]
    assert tiers[1].priority == 0
    assert tiers[1].terms == ["minimize_peak_import"]
    assert tiers[2].priority == 1
    assert tiers[2].terms == ["minimize_energy_cost"]


@pytest.mark.asyncio
async def test_tier_evidence_single_tier_for_single_priority_no_soft_constraints() -> None:
    adapter, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context = energy_context_for()
    scenario = OptimizationScenario(
        id="single-tier-evidence-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.5, latest_slot=5)],
        constraints=[
            Constraint(type="max_house_power", value=3, unit="kW"),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.solver_evidence is not None
    assert len(result.solver_evidence.tiers) == 1
    assert result.solver_evidence.tiers[0].terms == ["minimize_energy_cost"]


@pytest.mark.asyncio
async def test_tier_evidence_legacy_path_reports_one_implicit_tier() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    service = OptimizationService(registry, plan_service, CpSatOptimizer(registry))
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    scenario = OptimizationScenario(
        id="legacy-evidence-1",
        horizon=horizon,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=1)],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.solver_evidence is not None
    assert len(result.solver_evidence.tiers) == 1
    assert result.solver_evidence.tiers[0].terms == ["minimize_start"]


@pytest.mark.asyncio
async def test_solver_config_evidence_reports_name_version_and_determinism() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context = energy_context_for()
    scenario = OptimizationScenario(
        id="solver-config-evidence-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.5, latest_slot=5)],
        constraints=[Constraint(type="max_house_power", value=3, unit="kW")],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    evidence = result.solver_evidence
    assert evidence is not None
    assert evidence.solver_name == "cp-sat"
    assert evidence.solver_version
    assert evidence.num_search_workers == 1
    assert evidence.random_seed == 0


@pytest.mark.asyncio
async def test_multi_slot_optimization_produces_one_plan_per_distinct_time() -> None:
    _, registry, service = await build_context()
    switches = [device.id for device in registry.devices if device.type.value == "switch"]
    device_id = switches[0]
    horizon = energy_horizon(slots=3, resolution_minutes=15)
    context = EnergyContext(
        horizon=horizon,
        tariffs=[TariffPoint(slot=slot, price_per_kwh=0.10, currency="EUR") for slot in range(3)],
        solar_forecast=[SolarForecastPoint(slot=slot, power=0.0) for slot in range(3)],
        battery=None,
        source_revision="multi-slot-scenario",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    scenario = OptimizationScenario(
        id="multi-slot-plans-1",
        horizon=horizon,
        energy_context=context,
        loads=[
            flexible_load(
                device_id, load_id="load-a", power_kw=1.0, earliest_slot=0, latest_slot=0
            ),
            flexible_load(
                device_id, load_id="load-b", power_kw=1.0, earliest_slot=2, latest_slot=2
            ),
        ],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert len(result.plans) == 2
    assert result.plan == result.plans[0]
    first, second = result.plans
    assert first.execute_at is not None
    assert second.execute_at is not None
    assert first.execute_at < second.execute_at
    assert first.execute_at == horizon.start
    assert second.execute_at == horizon.start + timedelta(minutes=2 * 15)
    assert [command.id for command in first.commands] == ["multi-slot-plans-1:load-a"]
    assert [command.id for command in second.commands] == ["multi-slot-plans-1:load-b"]


@pytest.mark.asyncio
async def test_single_slot_optimization_still_produces_exactly_one_plan() -> None:
    adapter, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context = energy_context_for()
    scenario = OptimizationScenario(
        id="single-slot-plans-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.5, latest_slot=5)],
        constraints=[Constraint(type="max_house_power", value=3, unit="kW")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert len(result.plans) == 1
    assert result.plan == result.plans[0]
    del adapter


@pytest.mark.asyncio
async def test_legacy_path_also_populates_plans() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    scenario = OptimizationScenario(
        id="legacy-plans-1",
        horizon=horizon,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=1)],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert len(result.plans) == 1
    assert result.plan == result.plans[0]


@pytest.mark.asyncio
async def test_omitted_base_load_behaves_like_zero() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _single_slot_scenario(base_load_kw=None)
    scenario = OptimizationScenario(
        id="base-load-absent-1",
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=0)],
        constraints=[Constraint(type="max_grid_import", value=10, unit="kW")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.constraint_summary["slots"][0]["grid_import_kw"] == pytest.approx(1.0)
    assert result.objective_values["energy_cost"] == pytest.approx(0.025)
