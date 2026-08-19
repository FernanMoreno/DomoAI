"""Compare a baseline optimization scenario against named hypothetical variations."""

from __future__ import annotations

from pydantic import Field

from domoai.domain.models import StrictModel
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus, OptimizerPort
from domoai.optimizer.scenario import OptimizationScenario

_SUCCESS_STATUSES = {OptimizationStatus.OPTIMAL, OptimizationStatus.FEASIBLE}


class VariationOutcome(StrictModel):
    result: OptimizationResult
    diff: dict[str, float] = Field(default_factory=dict)


class CounterfactualResult(StrictModel):
    baseline: OptimizationResult
    variations: dict[str, VariationOutcome] = Field(default_factory=dict)


def _diff(variation: OptimizationResult, baseline: OptimizationResult) -> dict[str, float]:
    if variation.status not in _SUCCESS_STATUSES:
        return {}
    shared_keys = variation.objective_values.keys() & baseline.objective_values.keys()
    return {
        key: variation.objective_values[key] - baseline.objective_values[key]
        for key in shared_keys
    }


class CounterfactualAnalyzer:
    """Runs a baseline scenario plus named variations and reports how outcomes differ."""

    def __init__(self, optimizer: OptimizerPort) -> None:
        self.optimizer = optimizer

    def compare(
        self,
        baseline: OptimizationScenario,
        variations: dict[str, OptimizationScenario],
    ) -> CounterfactualResult:
        baseline_result = self.optimizer.optimize(baseline)
        if baseline_result.status not in _SUCCESS_STATUSES:
            return CounterfactualResult(baseline=baseline_result, variations={})

        outcomes: dict[str, VariationOutcome] = {}
        for name, scenario in variations.items():
            result = self.optimizer.optimize(scenario)
            outcomes[name] = VariationOutcome(result=result, diff=_diff(result, baseline_result))

        return CounterfactualResult(baseline=baseline_result, variations=outcomes)
