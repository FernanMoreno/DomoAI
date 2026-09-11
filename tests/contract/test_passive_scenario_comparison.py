from collections.abc import AsyncIterator

import pytest
from mcp.server.fastmcp import FastMCP

from domoai.application.optimization_worker import OptimizationWorker
from domoai.mcp.ortools_server import create_ortools_server
from domoai.optimizer.ports import OptimizationResult
from domoai.optimizer.scenario import Objective, OptimizationScenario, scenario_definition_digest
from tests.contract.test_ortools_mcp_contract import build_context, structured
from tests.integration.test_counterfactual_analysis import passive_energy_scenario


@pytest.fixture
async def passive_server_with_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[FastMCP, list[str]]]:
    context = await build_context()
    worker = OptimizationWorker(context.optimization_service)
    context.optimization_worker = worker
    calls: list[str] = []
    optimize = worker.optimize

    async def recorded_optimize(scenario: OptimizationScenario) -> OptimizationResult:
        calls.append(scenario_definition_digest(scenario))
        return await optimize(scenario)

    monkeypatch.setattr(worker, "optimize", recorded_optimize)
    try:
        yield create_ortools_server(context), calls
    finally:
        worker.close()


@pytest.fixture
def passive_server(passive_server_with_calls: tuple[FastMCP, list[str]]) -> FastMCP:
    return passive_server_with_calls[0]


async def test_public_passive_comparison_preserves_objectives_and_non_executable_summary(
    passive_server: FastMCP,
) -> None:
    baseline = passive_energy_scenario("public-base")
    alternative = passive_energy_scenario("public-alternative", price=0.4)

    comparison = structured(await passive_server.call_tool("compare_scenarios", {
        "baseline": baseline.model_dump(mode="json"),
        "variations": {"higher_tariff": alternative.model_dump(mode="json")},
    }))

    assert comparison["schema_version"] == "v1"
    summary = comparison["baseline"]
    assert summary["scenario_id"] == baseline.id
    assert summary["definition_digest"] == scenario_definition_digest(baseline)
    assert summary["status"] == "no_action_required"
    assert summary["proposal_id"] is None
    assert summary["next_step"] == "no_action_required"
    assert summary["objective_values"]["energy_cost"] == pytest.approx(0.1)
    outcome = comparison["variations"]["higher_tariff"]
    assert outcome["scenario_id"] == alternative.id
    assert outcome["status"] == "no_action_required"
    assert outcome["summary"]["proposal_id"] is None
    assert outcome["summary"]["definition_digest"] == scenario_definition_digest(alternative)
    assert outcome["summary"]["next_step"] == "no_action_required"
    assert outcome["diff"]["energy_cost"] == pytest.approx(0.1)
    assert "commands" not in str(comparison)


async def test_public_invalid_alternative_preserves_diagnostics_without_diff(
    passive_server: FastMCP,
) -> None:
    baseline = passive_energy_scenario("valid-base")
    invalid = baseline.model_copy(update={
        "id": "invalid-alternative",
        "objectives": [Objective(name="unsupported-test-objective", direction="minimize")],
    })

    comparison = structured(await passive_server.call_tool("compare_scenarios", {
        "baseline": baseline.model_dump(mode="json"),
        "variations": {"invalid": invalid.model_dump(mode="json")},
    }))

    assert comparison["baseline"]["status"] == "no_action_required"
    outcome = comparison["variations"]["invalid"]
    assert outcome["status"] == "invalid"
    assert outcome["diff"] == {}
    assert outcome["summary"]["diagnostic_codes"] == ["unsupported_objective"]
    assert outcome["summary"]["next_step"] == "revise_scenario_or_inputs"


async def test_public_same_id_invalid_alternative_does_not_replace_valid_baseline(
    passive_server_with_calls: tuple[FastMCP, list[str]],
) -> None:
    server, calls = passive_server_with_calls
    baseline = passive_energy_scenario("same-id")
    invalid = baseline.model_copy(update={
        "objectives": [Objective(name="unsupported-test-objective", direction="minimize")],
    })

    comparison = structured(await server.call_tool("compare_scenarios", {
        "baseline": baseline.model_dump(mode="json"),
        "variations": {"invalid": invalid.model_dump(mode="json")},
    }))

    assert comparison["baseline"]["status"] == "no_action_required"
    assert comparison["baseline"]["definition_digest"] == scenario_definition_digest(baseline)
    assert comparison["baseline"]["objective_values"]["energy_cost"] == pytest.approx(0.1)
    assert comparison["variations"]["invalid"]["status"] == "invalid"
    assert comparison["variations"]["invalid"]["diff"] == {}
    assert comparison["variations"]["invalid"]["summary"]["definition_digest"] == (
        scenario_definition_digest(invalid)
    )
    assert calls == [scenario_definition_digest(baseline)]


@pytest.mark.parametrize("invalid_first", [False, True])
async def test_public_same_id_variations_keep_independent_validity_and_digests(
    passive_server_with_calls: tuple[FastMCP, list[str]], invalid_first: bool,
) -> None:
    server, calls = passive_server_with_calls
    baseline = passive_energy_scenario("baseline")
    valid = passive_energy_scenario("shared-alternative-id", price=0.4)
    invalid = valid.model_copy(update={
        "objectives": [Objective(name="unsupported-test-objective", direction="minimize")],
    })
    alternatives = {"invalid": invalid, "valid": valid} if invalid_first else {
        "valid": valid, "invalid": invalid,
    }

    comparison = structured(await server.call_tool("compare_scenarios", {
        "baseline": baseline.model_dump(mode="json"),
        "variations": {name: value.model_dump(mode="json") for name, value in alternatives.items()},
    }))

    valid_outcome = comparison["variations"]["valid"]
    assert valid_outcome["status"] == "no_action_required"
    assert valid_outcome["diff"]["energy_cost"] == pytest.approx(0.1)
    assert valid_outcome["summary"]["definition_digest"] == scenario_definition_digest(valid)
    invalid_outcome = comparison["variations"]["invalid"]
    assert invalid_outcome["status"] == "invalid"
    assert invalid_outcome["diff"] == {}
    assert invalid_outcome["summary"]["definition_digest"] == scenario_definition_digest(invalid)
    assert invalid_outcome["summary"]["diagnostic_codes"] == ["unsupported_objective"]
    assert calls == [scenario_definition_digest(baseline), scenario_definition_digest(valid)]


async def test_public_invalid_baseline_keeps_own_digest_and_skips_same_id_valid_alternative(
    passive_server_with_calls: tuple[FastMCP, list[str]],
) -> None:
    server, calls = passive_server_with_calls
    valid = passive_energy_scenario("same-id")
    baseline = valid.model_copy(update={
        "objectives": [Objective(name="unsupported-test-objective", direction="minimize")],
    })

    comparison = structured(await server.call_tool("compare_scenarios", {
        "baseline": baseline.model_dump(mode="json"),
        "variations": {"valid": valid.model_dump(mode="json")},
    }))

    assert comparison["baseline"]["status"] == "invalid"
    assert comparison["baseline"]["definition_digest"] == scenario_definition_digest(baseline)
    assert comparison["baseline"]["diagnostic_codes"] == ["unsupported_objective"]
    assert comparison["variations"] == {}
    assert calls == []


async def test_a12_public_currency_mismatch_preserves_physics_and_omission_reason(passive_server):
    baseline = passive_energy_scenario("a12-base")
    payload = passive_energy_scenario("a12-usd", price=0.4).model_dump(mode="json")
    for point in payload["energy_context"]["tariffs"]:
        point["currency"] = "USD"
    variation = OptimizationScenario.model_validate(payload)
    response = structured(
        await passive_server.call_tool(
            "compare_scenarios",
            {
                "baseline": baseline.model_dump(mode="json"),
                "variations": {"usd": variation.model_dump(mode="json")},
            },
        )
    )
    outcome = response["variations"]["usd"]
    assert outcome["status"] == "no_action_required"
    assert "energy_cost" not in outcome["diff"]
    comparison = outcome["summary"]["constraint_summary"]["accounting"]["comparison"]
    assert comparison["omitted_objectives"]["energy_cost"] == "currency_mismatch"
    assert comparison["omitted_objectives"]["terminal_soc_value_eur"] == "not_applicable"
    assert comparison["diff_units"]["peak_import_kw"] == "kW"
