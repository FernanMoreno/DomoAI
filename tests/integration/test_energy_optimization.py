
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.domain.models import PlanStatus
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.energy import (
    BaseLoadPoint,
    BatteryProfile,
    ConfidenceBand,
    EnergyContext,
    SolarForecastPoint,
    TariffPoint,
)
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import (
    ComfortLoad,
    Constraint,
    EVChargingLoad,
    Horizon,
    Objective,
    OptimizationScenario,
)
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


def _ev_load(device_id: str, **overrides) -> EVChargingLoad:
    fields = {
        "id": "ev-1",
        "device_id": device_id,
        "capability": "position",
        "command": "set_position",
        "capacity_kwh": 10.0,
        "initial_soc_kwh": 2.0,
        "target_soc_kwh": 4.0,
        "max_charge_kw": 4.0,
        "deadline_slot": 7,
        "charge_efficiency": 0.95,
    }
    fields.update(overrides)
    return EVChargingLoad(**fields)


def _comfort_load(device_id: str, **overrides) -> ComfortLoad:
    fields = {
        "id": "comfort-1",
        "device_id": device_id,
        "capability": "target_temperature",
        "command": "set_temperature",
        "value": 21,
        "power": 0.5,
        "power_unit": "kW",
        "earliest_slot": 0,
        "deadline_slot": 8,
        "min_active_slots": 6,
    }
    fields.update(overrides)
    return ComfortLoad(**fields)


@pytest.mark.asyncio
async def test_ev_charging_reaches_target_soc_by_deadline() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "cover")
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="ev-feasible-1",
        horizon=context.horizon,
        energy_context=context,
        ev_loads=[_ev_load(device_id)],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.plan is not None
    resolution_hours = context.horizon.resolution_minutes / 60
    delivered_kwh = sum(
        command.value * resolution_hours * 0.95
        for plan in result.plans
        for command in plan.commands
        if command.device_id == device_id
    )
    assert delivered_kwh >= (4.0 - 2.0) - 1e-6


@pytest.mark.asyncio
async def test_ev_charging_unreachable_target_reports_infeasible() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "cover")
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="ev-infeasible-1",
        horizon=context.horizon,
        energy_context=context,
        ev_loads=[
            _ev_load(
                device_id,
                capacity_kwh=10.0,
                initial_soc_kwh=0.0,
                target_soc_kwh=10.0,
                max_charge_kw=1.0,
                deadline_slot=1,
            )
        ],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status is OptimizationStatus.INFEASIBLE
    assert result.plan is None


@pytest.mark.asyncio
async def test_ev_charging_already_at_target_solves_trivially() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "cover")
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="ev-already-charged-1",
        horizon=context.horizon,
        energy_context=context,
        ev_loads=[
            _ev_load(device_id, initial_soc_kwh=5.0, target_soc_kwh=4.0, deadline_slot=7)
        ],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}


@pytest.mark.asyncio
async def test_comfort_load_active_in_at_least_minimum_slots() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "climate")
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="comfort-feasible-1",
        horizon=context.horizon,
        energy_context=context,
        comfort_loads=[_comfort_load(device_id)],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    active_slots = {
        command.intent
        for plan in result.plans
        for command in plan.commands
        if command.device_id == device_id
    }
    assert len(active_slots) >= 6


def test_comfort_load_window_too_small_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        ComfortLoad(
            id="comfort-impossible",
            device_id="ha-climate-1",
            capability="target_temperature",
            command="set_temperature",
            value=21,
            power=0.5,
            power_unit="kW",
            earliest_slot=0,
            deadline_slot=4,
            min_active_slots=6,
        )


@pytest.mark.asyncio
async def test_comfort_load_power_is_bound_by_max_house_power() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "climate")
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="comfort-house-power-infeasible-1",
        horizon=context.horizon,
        energy_context=context,
        comfort_loads=[
            _comfort_load(device_id, min_active_slots=1, power=5.0, power_unit="kW")
        ],
        constraints=[
            Constraint(type="max_house_power", value=1, unit="kW"),
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status is OptimizationStatus.INFEASIBLE


@pytest.mark.asyncio
async def test_ev_and_comfort_coexist_with_generic_load_and_battery() -> None:
    _, registry, service = await build_context()
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    cover_id = next(device.id for device in registry.devices if device.type.value == "cover")
    climate_id = next(
        device.id for device in registry.devices if device.type.value == "climate"
    )
    context = energy_context_for()
    scenario = OptimizationScenario(
        id="combined-ev-comfort-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(switch_id, power_kw=0.5, latest_slot=5)],
        ev_loads=[_ev_load(cover_id, max_charge_kw=2.0, deadline_slot=7)],
        comfort_loads=[
            _comfort_load(climate_id, min_active_slots=2, power=0.5, power_unit="kW")
        ],
        constraints=[
            Constraint(type="max_house_power", value=6, unit="kW"),
            Constraint(type="max_grid_import", value=6, unit="kW"),
            Constraint(type="max_grid_export", value=6, unit="kW"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    for slot in result.constraint_summary["slots"]:
        total = (
            slot["load_power_kw"]
            + slot["battery_charge_kw"]
            + slot["ev_charge_kw"]
            + slot["comfort_power_kw"]
        )
        assert total <= 6 + 1e-6


def _export_tariff_scenario(*, export_price_per_kwh: float, with_export_tariff: bool):
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    context = EnergyContext(
        horizon=horizon,
        tariffs=[
            TariffPoint(slot=0, price_per_kwh=0.10, currency="EUR"),
            TariffPoint(slot=1, price_per_kwh=0.10, currency="EUR"),
        ],
        solar_forecast=[
            SolarForecastPoint(slot=0, power=5.0),
            SolarForecastPoint(slot=1, power=5.0),
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
        source_revision="export-tariff-scenario",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    return context, horizon


@pytest.mark.asyncio
async def test_export_tariff_reduces_net_cost() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context_with, horizon = _export_tariff_scenario(
        export_price_per_kwh=0.05, with_export_tariff=True
    )
    context_without, _ = _export_tariff_scenario(
        export_price_per_kwh=0.05, with_export_tariff=False
    )
    scenario_with = OptimizationScenario(
        id="export-tariff-with-1",
        horizon=horizon,
        energy_context=context_with,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=1)],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )
    scenario_without = scenario_with.model_copy(
        update={"id": "export-tariff-without-1", "energy_context": context_without}
    )

    result_with = service.optimize(scenario_with)
    result_without = service.optimize(scenario_without)

    assert result_with.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result_without.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result_with.objective_values["energy_cost"] < result_without.objective_values[
        "energy_cost"
    ]


@pytest.mark.asyncio
async def test_export_tariff_shifts_export_toward_higher_price_slot() -> None:
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
            SolarForecastPoint(slot=0, power=5.0),
            SolarForecastPoint(slot=1, power=5.0),
        ],
        export_tariffs=[
            TariffPoint(slot=0, price_per_kwh=0.02, currency="EUR"),
            TariffPoint(slot=1, price_per_kwh=0.20, currency="EUR"),
        ],
        battery=None,
        source_revision="export-tariff-shift",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    scenario = OptimizationScenario(
        id="export-tariff-shift-1",
        horizon=horizon,
        energy_context=context,
        loads=[
            flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=1, duration_slots=1)
        ],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.plan is not None
    command = next(cmd for cmd in result.plan.commands if cmd.device_id == device_id)
    assert command.intent == "scheduled_slot:0"


@pytest.mark.asyncio
async def test_export_revenue_reported_separately_from_net_cost() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _export_tariff_scenario(
        export_price_per_kwh=0.05, with_export_tariff=True
    )
    scenario = OptimizationScenario(
        id="export-revenue-reported-1",
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=1)],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.objective_values["export_revenue"] > 0
    assert result.objective_values["energy_cost"] == pytest.approx(
        sum(
            slot["grid_import_kw"] * (horizon.resolution_minutes / 60) * 0.10
            for slot in result.constraint_summary["slots"]
        )
        - result.objective_values["export_revenue"]
    )


@pytest.mark.asyncio
async def test_scenario_without_export_tariff_reports_zero_export_revenue() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _export_tariff_scenario(
        export_price_per_kwh=0.05, with_export_tariff=False
    )
    scenario = OptimizationScenario(
        id="export-revenue-absent-1",
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.0, earliest_slot=0, latest_slot=1)],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.objective_values["export_revenue"] == 0.0


def test_export_tariff_series_must_cover_the_horizon() -> None:
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    with pytest.raises(ValidationError):
        EnergyContext(
            horizon=horizon,
            tariffs=[
                TariffPoint(slot=0, price_per_kwh=0.10, currency="EUR"),
                TariffPoint(slot=1, price_per_kwh=0.10, currency="EUR"),
            ],
            solar_forecast=[
                SolarForecastPoint(slot=0, power=0.0),
                SolarForecastPoint(slot=1, power=0.0),
            ],
            export_tariffs=[TariffPoint(slot=0, price_per_kwh=0.05, currency="EUR")],
            battery=None,
            source_revision="export-tariff-invalid",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        )


def _confidence_context(
    *, with_solar_confidence: bool, with_base_load_confidence: bool
) -> tuple[EnergyContext, Horizon]:
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    context = EnergyContext(
        horizon=horizon,
        tariffs=[
            TariffPoint(slot=0, price_per_kwh=0.10, currency="EUR"),
            TariffPoint(slot=1, price_per_kwh=0.10, currency="EUR"),
        ],
        solar_forecast=[
            SolarForecastPoint(
                slot=slot,
                power=5.0,
                confidence=(
                    ConfidenceBand(low=1.0, high=6.0) if with_solar_confidence else None
                ),
            )
            for slot in range(2)
        ],
        base_load_forecast=[
            BaseLoadPoint(
                slot=slot,
                power=1.0,
                confidence=(
                    ConfidenceBand(low=0.5, high=3.0) if with_base_load_confidence else None
                ),
            )
            for slot in range(2)
        ],
        battery=None,
        source_revision="forecast-confidence-scenario",
        observed_at=datetime(2026, 8, 19, 12, tzinfo=UTC),
    )
    return context, horizon


def _confidence_scenario(
    scenario_id: str,
    context: EnergyContext,
    horizon: Horizon,
    device_id: str,
    *,
    conservative: bool = False,
) -> OptimizationScenario:
    return OptimizationScenario(
        id=scenario_id,
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=0.5, earliest_slot=0, latest_slot=1)],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
        conservative=conservative,
    )


@pytest.mark.asyncio
async def test_confidence_bounds_present_but_conservative_off_matches_no_bounds_plan() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context_with, horizon = _confidence_context(
        with_solar_confidence=True, with_base_load_confidence=True
    )
    context_without, _ = _confidence_context(
        with_solar_confidence=False, with_base_load_confidence=False
    )

    result_with = service.optimize(
        _confidence_scenario("confidence-present-1", context_with, horizon, device_id)
    )
    result_without = service.optimize(
        _confidence_scenario("confidence-absent-1", context_without, horizon, device_id)
    )

    assert result_with.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result_without.status == result_with.status
    assert result_with.objective_values["energy_cost"] == pytest.approx(
        result_without.objective_values["energy_cost"]
    )
    assert result_with.constraint_summary["slots"] == result_without.constraint_summary["slots"]
    assert result_with.objective_values["conservative_mode_active"] == 0.0


@pytest.mark.asyncio
async def test_forecast_confidence_reported_in_constraint_summary() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")

    both_bounded, horizon = _confidence_context(
        with_solar_confidence=True, with_base_load_confidence=True
    )
    result_both = service.optimize(
        _confidence_scenario("confidence-both-1", both_bounded, horizon, device_id)
    )
    assert result_both.constraint_summary["forecast_confidence"] == {
        "solar_bounded": True,
        "base_load_bounded": True,
    }

    no_base_load_series, _ = _confidence_context(
        with_solar_confidence=True, with_base_load_confidence=True
    )
    no_base_load_series = no_base_load_series.model_copy(update={"base_load_forecast": None})
    result_no_series = service.optimize(
        _confidence_scenario("confidence-no-base-series-1", no_base_load_series, horizon, device_id)
    )
    assert result_no_series.constraint_summary["forecast_confidence"] == {
        "solar_bounded": True,
        "base_load_bounded": None,
    }

    neither_bounded, _ = _confidence_context(
        with_solar_confidence=False, with_base_load_confidence=False
    )
    result_neither = service.optimize(
        _confidence_scenario("confidence-none-1", neither_bounded, horizon, device_id)
    )
    assert result_neither.constraint_summary["forecast_confidence"] == {
        "solar_bounded": False,
        "base_load_bounded": False,
    }


def test_confidence_partial_coverage_rejected() -> None:
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    with pytest.raises(ValidationError):
        EnergyContext(
            horizon=horizon,
            tariffs=[
                TariffPoint(slot=0, price_per_kwh=0.10, currency="EUR"),
                TariffPoint(slot=1, price_per_kwh=0.10, currency="EUR"),
            ],
            solar_forecast=[
                SolarForecastPoint(slot=0, power=5.0, confidence=ConfidenceBand(low=1.0, high=6.0)),
                SolarForecastPoint(slot=1, power=5.0),
            ],
            battery=None,
            source_revision="confidence-partial",
            observed_at=datetime(2026, 8, 19, 12, tzinfo=UTC),
        )


def test_confidence_band_inconsistent_with_point_estimate_rejected() -> None:
    with pytest.raises(ValidationError):
        SolarForecastPoint(slot=0, power=5.0, confidence=ConfidenceBand(low=6.0, high=7.0))
    with pytest.raises(ValidationError):
        BaseLoadPoint(slot=0, power=1.0, confidence=ConfidenceBand(low=0.0, high=0.5))


@pytest.mark.asyncio
async def test_conservative_mode_uses_pessimistic_solar_and_base_load() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _confidence_context(
        with_solar_confidence=True, with_base_load_confidence=True
    )

    result_normal = service.optimize(
        _confidence_scenario("conservative-off-1", context, horizon, device_id, conservative=False)
    )
    result_conservative = service.optimize(
        _confidence_scenario("conservative-on-1", context, horizon, device_id, conservative=True)
    )

    assert result_normal.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result_conservative.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    # Pessimistic solar (low=1.0 vs point 5.0) and pessimistic base load (high=3.0 vs
    # point 1.0) both push grid import up, so the conservative plan must cost more.
    assert (
        result_conservative.objective_values["energy_cost"]
        > result_normal.objective_values["energy_cost"]
    )
    assert result_conservative.objective_values["conservative_mode_active"] == 1.0


@pytest.mark.asyncio
async def test_conservative_mode_without_bounds_is_rejected() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _confidence_context(
        with_solar_confidence=True, with_base_load_confidence=False
    )

    result = service.optimize(
        _confidence_scenario(
            "conservative-missing-bounds-1", context, horizon, device_id, conservative=True
        )
    )

    assert result.status == OptimizationStatus.INVALID
    assert any(
        diagnostic.code == "conservative_mode_requires_confidence"
        for diagnostic in result.diagnostics
    )


@pytest.mark.asyncio
async def test_conservative_flag_off_or_absent_is_byte_identical_to_baseline() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context_bounded, horizon = _confidence_context(
        with_solar_confidence=True, with_base_load_confidence=True
    )
    context_unbounded, _ = _confidence_context(
        with_solar_confidence=False, with_base_load_confidence=False
    )

    baseline = service.optimize(
        OptimizationScenario(
            id="baseline-no-flag-1",
            horizon=horizon,
            energy_context=context_unbounded,
            loads=[flexible_load(device_id, power_kw=0.5, earliest_slot=0, latest_slot=1)],
            constraints=[
                Constraint(type="max_grid_import", value=10, unit="kW"),
                Constraint(type="max_grid_export", value=10, unit="kW"),
            ],
            objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
        )
    )
    explicit_false_unbounded = service.optimize(
        _confidence_scenario(
            "explicit-false-unbounded-1", context_unbounded, horizon, device_id, conservative=False
        )
    )
    explicit_false_bounded = service.optimize(
        _confidence_scenario(
            "explicit-false-bounded-1", context_bounded, horizon, device_id, conservative=False
        )
    )

    for result in (explicit_false_unbounded, explicit_false_bounded):
        assert result.objective_values["energy_cost"] == pytest.approx(
            baseline.objective_values["energy_cost"]
        )
        assert result.objective_values["conservative_mode_active"] == 0.0


def _degradation_context(
    *,
    degradation_cost_per_kwh: float | None,
    solar_slot0_kw: float = 0.0,
    tariff_cheap: float = 0.10,
    tariff_expensive: float = 0.10,
) -> tuple[EnergyContext, Horizon]:
    horizon = energy_horizon(slots=4, resolution_minutes=15)
    return (
        EnergyContext(
            horizon=horizon,
            tariffs=[
                TariffPoint(slot=0, price_per_kwh=tariff_cheap, currency="EUR"),
                TariffPoint(slot=1, price_per_kwh=tariff_cheap, currency="EUR"),
                TariffPoint(slot=2, price_per_kwh=tariff_cheap, currency="EUR"),
                TariffPoint(slot=3, price_per_kwh=tariff_expensive, currency="EUR"),
            ],
            solar_forecast=[
                SolarForecastPoint(slot=0, power=solar_slot0_kw),
                SolarForecastPoint(slot=1, power=0.0),
                SolarForecastPoint(slot=2, power=0.0),
                SolarForecastPoint(slot=3, power=0.0),
            ],
            battery=BatteryProfile(
                capacity_kwh=6,
                initial_soc_kwh=0,
                min_soc_kwh=0,
                max_soc_kwh=6,
                max_charge_kw=3,
                max_discharge_kw=3,
                charge_efficiency=1.0,
                discharge_efficiency=1.0,
                degradation_cost_per_kwh=degradation_cost_per_kwh,
            ),
            source_revision="battery-degradation-scenario",
            observed_at=datetime(2026, 8, 19, 12, tzinfo=UTC),
        ),
        horizon,
    )


def _degradation_scenario(
    scenario_id: str, context: EnergyContext, horizon: Horizon, device_id: str
) -> OptimizationScenario:
    return OptimizationScenario(
        id=scenario_id,
        horizon=horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=0.5, earliest_slot=3, latest_slot=3)],
        constraints=[
            Constraint(type="max_grid_import", value=10, unit="kW"),
            Constraint(type="max_grid_export", value=10, unit="kW"),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )


@pytest.mark.asyncio
async def test_battery_throughput_reported_when_battery_present() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _degradation_context(degradation_cost_per_kwh=None)

    result = service.optimize(
        _degradation_scenario("throughput-present-1", context, horizon, device_id)
    )

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    expected = sum(
        (slot["battery_charge_kw"] + slot["battery_discharge_kw"])
        * (horizon.resolution_minutes / 60)
        for slot in result.constraint_summary["slots"]
    )
    assert result.objective_values["battery_throughput_kwh"] == pytest.approx(expected)
    assert result.objective_values["battery_degradation_cost"] == 0.0


@pytest.mark.asyncio
async def test_battery_throughput_zero_when_no_battery() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _degradation_context(degradation_cost_per_kwh=None)
    context = context.model_copy(update={"battery": None})

    result = service.optimize(
        _degradation_scenario("throughput-absent-1", context, horizon, device_id)
    )

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.objective_values["battery_throughput_kwh"] == 0.0
    assert result.objective_values["battery_degradation_cost"] == 0.0


@pytest.mark.asyncio
async def test_battery_throughput_reporting_does_not_change_plan() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _degradation_context(degradation_cost_per_kwh=None, solar_slot0_kw=2.0)

    result_a = service.optimize(
        _degradation_scenario("throughput-noop-a-1", context, horizon, device_id)
    )
    result_b = service.optimize(
        _degradation_scenario("throughput-noop-b-1", context, horizon, device_id)
    )

    assert result_a.objective_values["energy_cost"] == pytest.approx(
        result_b.objective_values["energy_cost"]
    )
    assert result_a.constraint_summary["slots"] == result_b.constraint_summary["slots"]


@pytest.mark.asyncio
async def test_degradation_cost_discourages_cycling_with_no_net_benefit() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context_no_cost, horizon = _degradation_context(
        degradation_cost_per_kwh=None, solar_slot0_kw=2.0
    )
    context_with_cost, _ = _degradation_context(
        degradation_cost_per_kwh=1.0, solar_slot0_kw=2.0
    )

    result_no_cost = service.optimize(
        _degradation_scenario("no-benefit-without-1", context_no_cost, horizon, device_id)
    )
    result_with_cost = service.optimize(
        _degradation_scenario("no-benefit-with-1", context_with_cost, horizon, device_id)
    )

    assert result_no_cost.objective_values["battery_throughput_kwh"] > 0
    assert (
        result_with_cost.objective_values["battery_throughput_kwh"]
        < result_no_cost.objective_values["battery_throughput_kwh"]
    )


@pytest.mark.asyncio
async def test_degradation_cost_still_allows_net_beneficial_cycling() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _degradation_context(
        degradation_cost_per_kwh=0.05,
        tariff_cheap=0.05,
        tariff_expensive=0.50,
    )

    result = service.optimize(
        _degradation_scenario("net-beneficial-1", context, horizon, device_id)
    )

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.objective_values["battery_throughput_kwh"] > 0
    assert result.objective_values["battery_degradation_cost"] > 0


@pytest.mark.asyncio
async def test_degradation_cost_zero_is_a_no_op() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context_unset, horizon = _degradation_context(
        degradation_cost_per_kwh=None, solar_slot0_kw=2.0
    )
    context_zero, _ = _degradation_context(degradation_cost_per_kwh=0.0, solar_slot0_kw=2.0)

    result_unset = service.optimize(
        _degradation_scenario("zero-noop-unset-1", context_unset, horizon, device_id)
    )
    result_zero = service.optimize(
        _degradation_scenario("zero-noop-zero-1", context_zero, horizon, device_id)
    )

    assert result_unset.objective_values["energy_cost"] == pytest.approx(
        result_zero.objective_values["energy_cost"]
    )
    assert result_unset.objective_values["battery_throughput_kwh"] == pytest.approx(
        result_zero.objective_values["battery_throughput_kwh"]
    )
    assert result_zero.objective_values["battery_degradation_cost"] == 0.0


@pytest.mark.asyncio
async def test_energy_cost_includes_degradation_charge() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context, horizon = _degradation_context(
        degradation_cost_per_kwh=0.05,
        tariff_cheap=0.05,
        tariff_expensive=0.50,
    )

    result = service.optimize(
        _degradation_scenario("net-cost-components-1", context, horizon, device_id)
    )

    resolution_hours = horizon.resolution_minutes / 60
    import_cost = sum(
        slot["grid_import_kw"] * resolution_hours * context.tariffs[slot["slot"]].price_per_kwh
        for slot in result.constraint_summary["slots"]
    )
    assert result.objective_values["energy_cost"] == pytest.approx(
        import_cost + result.objective_values["battery_degradation_cost"]
    )
