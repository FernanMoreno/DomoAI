"""Currency evidence across real local provider, solver process and MCP boundaries."""

import hashlib

import pytest

from domoai.application.optimization_worker import WorkerBudget
from domoai.application.process_optimization_worker import ProcessOptimizationWorker
from domoai.mcp.ortools_server import create_ortools_server
from domoai.optimizer.base_load import DeclaredBaseLoadProfile, DeclaredBaseLoadProvider
from domoai.optimizer.providers import (
    ComposedEnergyContextProvider,
    StaticSolarForecastProvider,
    StaticTariffProvider,
)
from domoai.optimizer.scenario import OptimizationScenario
from tests.contract.test_ortools_mcp_contract import build_context, structured
from tests.fixtures.base_load import NOW, horizon, profile_payload, solar, tariffs


@pytest.mark.parametrize("currency", ["EUR", "USD"])
async def test_a12_composed_currency_survives_provider_process_mcp_product_comparison(currency):
    requested = horizon()
    tariff = tariffs(requested)
    for point in tariff.points:
        point.currency = currency
    provider = ComposedEnergyContextProvider(
        StaticTariffProvider(tariff),
        StaticSolarForecastProvider(solar(requested)),
        base_load=DeclaredBaseLoadProvider(
            DeclaredBaseLoadProfile.model_validate(profile_payload()),
            tenant_id="tenant-a",
            household_id="home-a",
        ),
        now=lambda: NOW,
    )
    energy = provider.get_context(requested)
    context = await build_context()
    worker = ProcessOptimizationWorker(context.registry, WorkerBudget(max_solver_time_seconds=10))
    context.optimization_worker = worker
    server = create_ortools_server(context)
    revision = context.runtime_revision
    scenario = OptimizationScenario(id="a12-composed", horizon=requested, energy_context=energy)
    original = scenario.model_dump_json()
    try:
        result = structured(
            await server.call_tool(
                "optimize_scenario",
                {"scenario": scenario.model_dump(mode="json"), "validate_proposal": False},
            )
        )
        assert result["status"] == "no_action_required", result
        assert result["objective_values"]["energy_cost"] == pytest.approx(0.4)
        block = result["constraint_summary"]["accounting"]
        assert block["currency"] == currency
        assert (
            block["source_revision_digest"]
            == hashlib.sha256(energy.source_revision.encode("utf-8")).hexdigest()
        )
        assert (
            result["constraint_summary"]["forecast_assumptions"]["base_load"]["kind"]
            == "declared_reserve"
        )
        assert "solar_accounting" in result["constraint_summary"]
        summary = structured(await server.call_tool("summarize_solution", {"result": result}))
        assert summary["constraint_summary"]["accounting"] == block
        assert (
            summary["constraint_summary"]["forecast_assumptions"]
            == result["constraint_summary"]["forecast_assumptions"]
        )
        comparison = structured(
            await server.call_tool(
                "compare_scenarios",
                {
                    "baseline": scenario.model_dump(mode="json"),
                    "variations": {"same": scenario.model_dump(mode="json")},
                },
            )
        )
        outcome = comparison["variations"]["same"]
        assert outcome["diff"]["energy_cost"] == 0
        assert (
            outcome["summary"]["constraint_summary"]["accounting"]["comparison"]["diff_units"][
                "energy_cost"
            ]
            == currency
        )
        assert context.runtime_revision == revision
        assert scenario.model_dump_json() == original
        assert result["plan"] is None and result["plans"] == []
    finally:
        worker.close()
