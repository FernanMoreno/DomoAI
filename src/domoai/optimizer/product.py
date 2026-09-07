"""Deterministic product-facing projections for optimizer results."""

from typing import Literal

from domoai.domain.product import ProductAlternative, ProductSummary
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus

_SUCCESS_STATUSES = frozenset(
    {
        OptimizationStatus.OPTIMAL,
        OptimizationStatus.FEASIBLE,
        OptimizationStatus.OPTIMAL_HIERARCHY,
        OptimizationStatus.FEASIBLE_HIERARCHY,
    }
)


def build_product_summary(result: OptimizationResult) -> ProductSummary:
    """Create a bounded summary without exposing commands or mutating state."""

    hard_satisfied = result.constraint_summary.get("hard_satisfied")
    if not isinstance(hard_satisfied, bool):
        hard_satisfied = None
    forecast_confidence = result.constraint_summary.get("forecast_confidence")
    if not isinstance(forecast_confidence, str):
        forecast_confidence = None

    alternatives = [
        ProductAlternative(
            plan_id=plan_id,
            objective_values=evidence.objective_values,
            constraint_effects=evidence.constraint_effects,
            forecast_assumptions=evidence.forecast_assumptions,
        )
        for plan_id, evidence in sorted(result.alternative_evidence.items())
    ]
    compact_constraints = {
        key: result.constraint_summary[key]
        for key in ("hard_satisfied", "soft_violations", "forecast_confidence")
        if key in result.constraint_summary
    }
    next_step: Literal[
        "validate_and_request_approval_before_execution",
        "revise_scenario_or_inputs",
        "no_action_required",
    ]
    if result.status in _SUCCESS_STATUSES and result.plan is not None:
        headline = (
            "Proposal satisfies declared hard constraints."
            if hard_satisfied is True
            else "Proposal requires review because hard-constraint evidence is incomplete."
        )
        next_step = "validate_and_request_approval_before_execution"
        proposal_id = result.plan.id
    elif result.status is OptimizationStatus.NO_ACTION_REQUIRED:
        headline = "No physical action is required for this valid scenario."
        next_step = "no_action_required"
        proposal_id = None
    else:
        headline = f"No proposal was produced because the result is {result.status.value}."
        next_step = "revise_scenario_or_inputs"
        proposal_id = None

    return ProductSummary(
        scenario_id=result.scenario_id,
        definition_digest=result.definition_digest,
        status=result.status.value,
        headline=headline,
        proposal_id=proposal_id,
        objective_values=dict(sorted(result.objective_values.items())),
        constraint_summary=compact_constraints,
        alternatives=alternatives,
        hard_constraints_satisfied=hard_satisfied,
        forecast_confidence=forecast_confidence,
        diagnostic_codes=sorted({diagnostic.code for diagnostic in result.diagnostics}),
        next_step=next_step,
    )


__all__ = ["build_product_summary"]
