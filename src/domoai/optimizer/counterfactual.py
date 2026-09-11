"""Compare a baseline optimization scenario against named hypothetical variations."""

from __future__ import annotations

from typing import Protocol, cast

from pydantic import Field

from domoai.domain.models import StrictModel
from domoai.domain.product import (
    PHYSICAL_OBJECTIVE_UNITS,
    AccountingEvidence,
    ComparisonAccounting,
    OmissionReason,
    objective_comparison_omission,
    validate_accounting,
)
from domoai.optimizer.ports import (
    OptimizationResult,
    OptimizationStatus,
    OptimizerPort,
)
from domoai.optimizer.scenario import OptimizationScenario

_SUCCESS_STATUSES = {
    OptimizationStatus.OPTIMAL,
    OptimizationStatus.FEASIBLE,
    OptimizationStatus.OPTIMAL_HIERARCHY,
    OptimizationStatus.FEASIBLE_HIERARCHY,
    OptimizationStatus.NO_ACTION_REQUIRED,
}


class VariationOutcome(StrictModel):
    result: OptimizationResult
    diff: dict[str, float] = Field(default_factory=dict)


class CounterfactualResult(StrictModel):
    baseline: OptimizationResult
    variations: dict[str, VariationOutcome] = Field(default_factory=dict)


class ScenarioComparisonRequest(StrictModel):
    """Bounded public request for named scenario comparisons."""

    baseline: OptimizationScenario
    variations: dict[str, OptimizationScenario] = Field(default_factory=dict, max_length=16)


class AsyncOptimizerPort(Protocol):
    async def optimize(self, scenario: OptimizationScenario) -> OptimizationResult: ...


def _comparison_outcome(
    variation: OptimizationResult, baseline: OptimizationResult
) -> VariationOutcome:
    if variation.status not in _SUCCESS_STATUSES:
        return VariationOutcome(result=variation, diff={})
    shared_keys = variation.objective_values.keys() & baseline.objective_values.keys()
    if len(shared_keys) > 32:
        raise ValueError("comparison accounting supports at most 32 shared objectives")
    current = validate_accounting(variation.constraint_summary, variation.objective_values)
    base = validate_accounting(baseline.constraint_summary, baseline.objective_values)
    units = (
        current.objective_units
        if current is not None
        else {
            key: unit
            for key, unit in PHYSICAL_OBJECTIVE_UNITS.items()
            if key in variation.objective_values
        }
    )
    diff: dict[str, float] = {}
    diff_units: dict[str, str] = {}
    omitted: dict[str, OmissionReason] = {}
    for key in sorted(shared_keys):
        unit = units.get(key)
        reason = objective_comparison_omission(
            key,
            current,
            base,
            variation_constraints=variation.constraint_summary,
            baseline_constraints=baseline.constraint_summary,
        )
        if reason is not None:
            omitted[key] = reason
        else:
            assert unit is not None
            diff[key] = variation.objective_values[key] - baseline.objective_values[key]
            diff_units[key] = unit
    evidence = (
        current.model_copy(deep=True)
        if current is not None
        else AccountingEvidence(
            objective_units=units,
        )
    )
    evidence.comparison = ComparisonAccounting(
        baseline_scenario_id=baseline.scenario_id,
        baseline_definition_digest=baseline.definition_digest,
        diff_units=diff_units,
        omitted_objectives=omitted,
    )
    enriched = variation.model_copy(deep=True)
    enriched.constraint_summary["accounting"] = evidence.model_dump(mode="json")
    validate_accounting(enriched.constraint_summary, enriched.objective_values)
    return VariationOutcome(result=enriched, diff=diff)


def _diff(variation: OptimizationResult, baseline: OptimizationResult) -> dict[str, float]:
    """Retain the existing diff helper with the same accounting rules as comparison."""
    return _comparison_outcome(variation, baseline).diff


class CounterfactualAnalyzer:
    """Runs a baseline scenario plus named variations and reports how outcomes differ."""

    def __init__(self, optimizer: OptimizerPort | AsyncOptimizerPort) -> None:
        self.optimizer = optimizer

    def compare(
        self,
        baseline: OptimizationScenario,
        variations: dict[str, OptimizationScenario],
    ) -> CounterfactualResult:
        optimizer = cast(OptimizerPort, self.optimizer)
        baseline_result = optimizer.optimize(baseline)
        if baseline_result.status not in _SUCCESS_STATUSES:
            return CounterfactualResult(baseline=baseline_result, variations={})

        outcomes: dict[str, VariationOutcome] = {}
        for name, scenario in variations.items():
            result = optimizer.optimize(scenario)
            outcomes[name] = _comparison_outcome(result, baseline_result)

        return CounterfactualResult(baseline=baseline_result, variations=outcomes)

    async def compare_async(
        self,
        baseline: OptimizationScenario,
        variations: dict[str, OptimizationScenario],
    ) -> CounterfactualResult:
        """Run the same comparison through the bounded async worker boundary."""

        worker = cast(AsyncOptimizerPort, self.optimizer)
        if not hasattr(worker, "optimize"):
            raise TypeError("async counterfactual comparison requires an optimizer worker")
        baseline_result = await worker.optimize(baseline)
        if baseline_result.status not in _SUCCESS_STATUSES:
            return CounterfactualResult(baseline=baseline_result, variations={})

        outcomes: dict[str, VariationOutcome] = {}
        for name, scenario in variations.items():
            result = await worker.optimize(scenario)
            outcomes[name] = _comparison_outcome(result, baseline_result)
        return CounterfactualResult(baseline=baseline_result, variations=outcomes)
