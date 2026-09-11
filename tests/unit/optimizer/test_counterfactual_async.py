import pytest

from domoai.domain.models import ErrorDetail
from domoai.optimizer.counterfactual import CounterfactualAnalyzer, CounterfactualResult
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import Horizon, OptimizationScenario


def _scenario(identifier: str) -> OptimizationScenario:
    from datetime import UTC, datetime

    return OptimizationScenario(
        id=identifier,
        horizon=Horizon(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 1, 1, tzinfo=UTC),
            resolution_minutes=15,
            timezone="Europe/Madrid",
        ),
    )


class _Worker:
    async def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        return OptimizationResult(
            scenario_id=scenario.id,
            definition_digest=f"digest:{scenario.id}",
            status=(
                OptimizationStatus.INFEASIBLE
                if scenario.id == "bad"
                else OptimizationStatus.FEASIBLE
            ),
            solver="test",
            objective_values={"cost": 1.0 if scenario.id == "base" else 2.0},
        )


@pytest.mark.asyncio
async def test_async_counterfactual_reuses_bounded_worker_and_skips_after_bad_baseline() -> None:
    analyzer = CounterfactualAnalyzer(_Worker())

    result = await analyzer.compare_async(
        _scenario("base"), {"bad": _scenario("bad")},
    )

    assert result.baseline.status is OptimizationStatus.FEASIBLE
    assert result.variations["bad"].diff == {}

    stopped = await analyzer.compare_async(
        _scenario("bad"), {"never_run": _scenario("variation")},
    )
    assert stopped.baseline.status is OptimizationStatus.INFEASIBLE
    assert stopped.variations == {}


class _ResultsOptimizer:
    def __init__(self, results: dict[str, OptimizationResult]) -> None:
        self.results = results
        self.calls: list[str] = []

    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        self.calls.append(scenario.id)
        return self.results[scenario.id]


class _AsyncResultsOptimizer:
    def __init__(self, optimizer: _ResultsOptimizer) -> None:
        self.optimizer = optimizer

    async def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        return self.optimizer.optimize(scenario)


async def _compare_results(
    optimizer: _ResultsOptimizer, *, asynchronous: bool
) -> CounterfactualResult:
    baseline, alternatives = _scenario("base"), {"alternative": _scenario("alternative")}
    if asynchronous:
        return await CounterfactualAnalyzer(_AsyncResultsOptimizer(optimizer)).compare_async(
            baseline, alternatives
        )
    return CounterfactualAnalyzer(optimizer).compare(baseline, alternatives)


def _result(identifier: str, status: OptimizationStatus, **objectives: float) -> OptimizationResult:
    return OptimizationResult(
        scenario_id=identifier,
        definition_digest=f"digest:{identifier}",
        status=status,
        solver="controlled-result",
        objective_values=objectives,
        diagnostics=[ErrorDetail(code="retained_diagnostic", message="Retain result evidence")],
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("baseline_status", "alternative_status"),
    [
        (OptimizationStatus.NO_ACTION_REQUIRED, OptimizationStatus.NO_ACTION_REQUIRED),
        (OptimizationStatus.FEASIBLE, OptimizationStatus.NO_ACTION_REQUIRED),
        (OptimizationStatus.NO_ACTION_REQUIRED, OptimizationStatus.FEASIBLE),
    ],
)
async def test_passive_comparison_preserves_shared_objectives_and_result_evidence(
    asynchronous: bool,
    baseline_status: OptimizationStatus,
    alternative_status: OptimizationStatus,
) -> None:
    baseline = _result("base", baseline_status, cost=3, baseline_only=8)
    alternative = _result("alternative", alternative_status, cost=1, alternative_only=9)
    for result in (baseline, alternative):
        result.constraint_summary["accounting"] = {
            "version": 1,
            "currency": "EUR",
            "objective_units": {"cost": "EUR"},
        }
    originals = [result.model_dump(mode="json") for result in (baseline, alternative)]
    optimizer = _ResultsOptimizer({"base": baseline, "alternative": alternative})

    comparison = await _compare_results(optimizer, asynchronous=asynchronous)

    assert comparison.baseline == baseline
    enriched = comparison.variations["alternative"].result.model_dump(mode="json")
    enriched["constraint_summary"]["accounting"] = alternative.constraint_summary["accounting"]
    assert enriched == originals[1]
    assert [result.model_dump(mode="json") for result in (baseline, alternative)] == originals
    assert comparison.variations["alternative"].diff == {"cost": -2}
    assert optimizer.calls == ["base", "alternative"]


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_passive_comparison_does_not_invent_missing_objectives(asynchronous: bool) -> None:
    optimizer = _ResultsOptimizer({
        "base": _result("base", OptimizationStatus.NO_ACTION_REQUIRED),
        "alternative": _result("alternative", OptimizationStatus.NO_ACTION_REQUIRED, cost=1),
    })

    comparison = await _compare_results(optimizer, asynchronous=asynchronous)

    assert comparison.variations["alternative"].diff == {}
    assert optimizer.calls == ["base", "alternative"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failed_baseline", [False, True])
@pytest.mark.parametrize("status", [
    OptimizationStatus.INVALID,
    OptimizationStatus.INFEASIBLE,
    OptimizationStatus.TIMEOUT,
    OptimizationStatus.UNKNOWN,
])
async def test_failed_results_never_produce_diffs_or_evaluate_after_failed_baseline(
    asynchronous: bool, failed_baseline: bool, status: OptimizationStatus
) -> None:
    baseline = _result(
        "base", status if failed_baseline else OptimizationStatus.NO_ACTION_REQUIRED, cost=3
    )
    alternative = _result("alternative", status, cost=1)
    optimizer = _ResultsOptimizer({"base": baseline, "alternative": alternative})

    comparison = await _compare_results(optimizer, asynchronous=asynchronous)

    assert comparison.baseline == baseline
    if failed_baseline:
        assert comparison.variations == {}
        assert optimizer.calls == ["base"]
    else:
        assert comparison.variations["alternative"].result == alternative
        assert comparison.variations["alternative"].diff == {}
        assert optimizer.calls == ["base", "alternative"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("kind", ["same", "mismatch", "legacy", "partial", "extension", "solar"])
async def test_a12_comparison_requires_compatible_evidence_without_mutating_inputs(
    asynchronous, kind
):
    from tests.contract.test_phase4_product_contract import _a12_accounting

    objectives = {
        "energy_cost": 3,
        "peak_import_kw": 1,
        "terminal_soc_value_eur": 0,
        "extension": 2,
        "solar_self_consumption_kwh": 3,
    }
    baseline = _result("base", OptimizationStatus.NO_ACTION_REQUIRED, **objectives)
    alternative = _result(
        "alternative",
        OptimizationStatus.NO_ACTION_REQUIRED,
        **{**objectives, "energy_cost": 1, "peak_import_kw": 3, "extension": 3},
    )
    for result, currency in [
        (baseline, "EUR"),
        (alternative, "USD" if kind == "mismatch" else "EUR"),
    ]:
        result.constraint_summary["accounting"] = {
            **_a12_accounting(currency),
            "objective_units": {
                "energy_cost": currency,
                "peak_import_kw": "kW",
                "terminal_soc_value_eur": "EUR",
                "solar_self_consumption_kwh": "kWh",
            },
        }
    if kind == "legacy":
        baseline.constraint_summary = {}
    if kind == "partial":
        alternative.constraint_summary["accounting"]["objective_units"].pop("peak_import_kw")
    if kind == "extension":
        baseline.constraint_summary["accounting"]["objective_units"]["extension"] = "kWh"
        alternative.constraint_summary["accounting"]["objective_units"]["extension"] = "kW"
    if kind == "solar":
        baseline.constraint_summary["solar_accounting"] = {
            "convention": {"accounting_version": "legacy"}
        }
        alternative.constraint_summary["solar_accounting"] = {
            "convention": {"accounting_version": "v1"}
        }
    originals = [result.model_dump(mode="json") for result in (baseline, alternative)]
    optimizer = _ResultsOptimizer({"base": baseline, "alternative": alternative})
    comparison = await _compare_results(optimizer, asynchronous=asynchronous)
    outcome = comparison.variations["alternative"]
    block = outcome.result.constraint_summary["accounting"]["comparison"]
    assert block["baseline_scenario_id"] == "base"
    assert block["baseline_definition_digest"] == "digest:base"
    assert block["omitted_objectives"]["terminal_soc_value_eur"] == "not_applicable"
    assert block["omitted_objectives"]["extension"] == (
        "unit_mismatch" if kind == "extension" else "unit_unknown"
    )
    if kind in {"mismatch", "legacy"}:
        assert "energy_cost" not in outcome.diff
        assert block["omitted_objectives"]["energy_cost"] == (
            "currency_mismatch" if kind == "mismatch" else "currency_unknown"
        )
    else:
        assert outcome.diff["energy_cost"] == -2
        assert block["diff_units"]["energy_cost"] == "EUR"
    if kind == "partial":
        assert "peak_import_kw" not in outcome.diff
        assert block["omitted_objectives"]["peak_import_kw"] == "unit_unknown"
    else:
        assert outcome.diff["peak_import_kw"] == 2
        assert block["diff_units"]["peak_import_kw"] == "kW"
    if kind == "solar":
        assert "solar_self_consumption_kwh" not in outcome.diff
        assert (
            block["omitted_objectives"]["solar_self_consumption_kwh"]
            == "accounting_version_mismatch"
        )
    assert [result.model_dump(mode="json") for result in (baseline, alternative)] == originals
    assert outcome.result is not alternative


@pytest.mark.parametrize("count", [32, 33])
async def test_a12_shared_metric_limit_rejects_instead_of_truncating(count):
    values = {f"metric{i}": float(i) for i in range(count)}
    optimizer = _ResultsOptimizer(
        {
            name: _result(name, OptimizationStatus.NO_ACTION_REQUIRED, **values)
            for name in ("base", "alternative")
        }
    )
    if count == 33:
        with pytest.raises(ValueError, match="32"):
            await _compare_results(optimizer, asynchronous=True)
    else:
        result = await _compare_results(optimizer, asynchronous=True)
        outcome = result.variations["alternative"]
        assert outcome.diff == {}
        assert (
            len(outcome.result.constraint_summary["accounting"]["comparison"]["omitted_objectives"])
            == 32
        )


async def test_a12_comparison_replaces_prior_baseline_and_preserves_original_nested_evidence():
    from tests.contract.test_phase4_product_contract import _a12_accounting

    base = _result("base", OptimizationStatus.NO_ACTION_REQUIRED, energy_cost=4)
    other = _result("other", OptimizationStatus.NO_ACTION_REQUIRED, energy_cost=8)
    alternative = _result("alternative", OptimizationStatus.NO_ACTION_REQUIRED, energy_cost=1)
    for item in (base, other, alternative):
        item.constraint_summary["accounting"] = {
            **_a12_accounting(),
            "objective_units": {"energy_cost": "EUR"},
        }
    optimizer = _ResultsOptimizer({"base": base, "other": other, "alternative": alternative})
    analyzer = CounterfactualAnalyzer(optimizer)
    first = analyzer.compare(_scenario("base"), {"v": _scenario("alternative")})
    first_result = first.variations["v"].result
    snapshot = first_result.model_dump_json()
    optimizer.results["alternative"] = first_result
    second = analyzer.compare(_scenario("other"), {"v": _scenario("alternative")})
    assert second.variations["v"].diff == {"energy_cost": -7}
    assert (
        second.variations["v"].result.constraint_summary["accounting"]["comparison"][
            "baseline_scenario_id"
        ]
        == "other"
    )
    assert first_result.model_dump_json() == snapshot
    assert "comparison" not in alternative.constraint_summary["accounting"]


async def test_a12_known_monetary_unit_without_currency_omits_financial_delta():
    from tests.contract.test_phase4_product_contract import _a12_accounting

    results = {
        name: _result(name, OptimizationStatus.NO_ACTION_REQUIRED, energy_cost=1, extension=2)
        for name in ("base", "alternative")
    }
    for name, result in results.items():
        result.constraint_summary["accounting"] = {
            **_a12_accounting(),
            "currency": "EUR" if name == "base" else None,
            "objective_units": {"energy_cost": "EUR", "extension": "EUR"},
        }
    comparison = await _compare_results(_ResultsOptimizer(results), asynchronous=True)
    outcome = comparison.variations["alternative"]
    assert outcome.diff == {}
    assert outcome.result.constraint_summary["accounting"]["currency"] is None
    assert outcome.result.constraint_summary["accounting"]["comparison"]["omitted_objectives"] == {
        "energy_cost": "currency_unknown",
        "extension": "currency_unknown",
    }
