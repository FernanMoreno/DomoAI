"""Read-only product projections built from typed optimization evidence."""

from typing import Any, Literal

from pydantic import Field

from domoai.domain.models import StrictModel


class ProductAlternative(StrictModel):
    plan_id: str = Field(min_length=1, max_length=200)
    objective_values: dict[str, float] = Field(default_factory=dict, max_length=32)
    constraint_effects: dict[str, Any] = Field(default_factory=dict, max_length=16)
    forecast_assumptions: dict[str, Any] = Field(default_factory=dict, max_length=16)


class ProductSummary(StrictModel):
    """Stable, non-executable explanation of an optimization proposal."""

    schema_version: Literal["v1"] = "v1"
    scenario_id: str = Field(min_length=1, max_length=200)
    definition_digest: str | None = None
    status: str = Field(min_length=1, max_length=64)
    headline: str = Field(min_length=1, max_length=240)
    proposal_id: str | None = Field(default=None, max_length=200)
    objective_values: dict[str, float] = Field(default_factory=dict, max_length=32)
    constraint_summary: dict[str, Any] = Field(default_factory=dict, max_length=16)
    alternatives: list[ProductAlternative] = Field(default_factory=list, max_length=16)
    hard_constraints_satisfied: bool | None = None
    forecast_confidence: str | None = Field(default=None, max_length=32)
    diagnostic_codes: list[str] = Field(default_factory=list, max_length=32)
    next_step: Literal[
        "validate_and_request_approval_before_execution",
        "revise_scenario_or_inputs",
        "no_action_required",
    ]


class ScenarioComparisonVariation(StrictModel):
    """One named, non-executable counterfactual projection."""

    scenario_id: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=64)
    summary: ProductSummary
    diff: dict[str, float] = Field(default_factory=dict, max_length=32)


class ScenarioComparison(StrictModel):
    """Bounded product projection for a baseline and named variations."""

    schema_version: Literal["v1"] = "v1"
    runtime_revision: str = Field(min_length=1)
    baseline: ProductSummary
    variations: dict[str, ScenarioComparisonVariation] = Field(
        default_factory=dict, max_length=16
    )


__all__ = [
    "ProductAlternative",
    "ProductSummary",
    "ScenarioComparison",
    "ScenarioComparisonVariation",
]
