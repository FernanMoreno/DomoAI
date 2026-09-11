"""Read-only product projections built from typed optimization evidence."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, StringConstraints, field_validator, model_validator

from domoai.domain.models import StrictModel

# Closed semantics for existing metrics; extensions require explicit unit evidence.
PHYSICAL_OBJECTIVE_UNITS = {
    "peak_import_kw": "kW",
    "solar_self_consumption_kwh": "kWh",
    "battery_throughput_kwh": "kWh",
    "terminal_soc_kwh": "kWh",
    "start_slot_sum": "slot",
    "conservative_mode_active": "1",
}
MONETARY_OBJECTIVES = frozenset({"energy_cost", "export_revenue", "battery_degradation_cost"})
ObjectiveKey = Annotated[
    str, StringConstraints(min_length=1, max_length=64, strip_whitespace=False)
]
ObjectiveUnit = Annotated[
    str, StringConstraints(pattern=r"^(kWh|kW|slot|1|[A-Z]{3})$", strip_whitespace=False)
]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$", strip_whitespace=False)]
OmissionReason = Literal[
    "currency_mismatch",
    "currency_unknown",
    "unit_mismatch",
    "unit_unknown",
    "not_applicable",
    "accounting_version_mismatch",
]


class ComparisonAccounting(StrictModel):
    """Units and omissions for one identified variation minus baseline."""

    baseline_scenario_id: str = Field(min_length=1, max_length=200)
    baseline_definition_digest: str | None = None
    diff_units: dict[ObjectiveKey, ObjectiveUnit] = Field(default_factory=dict, max_length=32)
    omitted_objectives: dict[ObjectiveKey, OmissionReason] = Field(
        default_factory=dict, max_length=32
    )

    @model_validator(mode="after")
    def validate_disjoint(self) -> ComparisonAccounting:
        if self.diff_units.keys() & self.omitted_objectives.keys():
            raise ValueError("compared objectives must not also be omitted")
        if len(self.diff_units.keys() | self.omitted_objectives.keys()) > 32:
            raise ValueError("comparison accounting exceeds 32 shared objectives")
        return self


class AccountingEvidence(StrictModel):
    """Versioned accounting metadata inside existing result dictionaries."""

    version: Literal[1] = 1
    currency: Currency | None = None
    source_revision_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    objective_units: dict[ObjectiveKey, ObjectiveUnit] = Field(default_factory=dict, max_length=32)
    degradation_currency_source: Literal["context_inherited", "not_configured"] = "not_configured"
    terminal_value_applied: StrictBool = False
    comparison: ComparisonAccounting | None = None

    @field_validator("version", mode="before")
    @classmethod
    def validate_version_type(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("accounting version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_currency(self) -> AccountingEvidence:
        if self.terminal_value_applied and self.currency != "EUR":
            raise ValueError("applied terminal valuation requires EUR accounting")
        return self


def validate_accounting(
    container: dict[str, Any], objective_values: dict[str, float]
) -> AccountingEvidence | None:
    """Reject corrupt explicit evidence; absent evidence remains legacy unknown."""

    if "accounting" not in container:
        return None
    evidence = AccountingEvidence.model_validate(container["accounting"])
    if not evidence.objective_units.keys() <= objective_values.keys():
        raise ValueError("accounting units refer to missing objective values")
    for key, unit in evidence.objective_units.items():
        expected = PHYSICAL_OBJECTIVE_UNITS.get(key)
        if key in MONETARY_OBJECTIVES:
            expected = evidence.currency
            if unit in {"kWh", "kW", "slot", "1"}:
                raise ValueError("monetary objective requires a monetary unit")
        elif key == "terminal_soc_value_eur":
            expected = "EUR"
        if expected is not None and expected != unit:
            raise ValueError("accounting objective unit contradicts its metric")
        if unit not in {"kWh", "kW", "slot", "1"} and key != "terminal_soc_value_eur":
            if evidence.currency is not None and unit != evidence.currency:
                raise ValueError("monetary objective unit contradicts accounting currency")
        if not math.isfinite(objective_values[key]):
            raise ValueError("accounted objective values must be finite")
    if (
        not evidence.terminal_value_applied
        and evidence.objective_units.get("terminal_soc_value_eur") == "EUR"
        and objective_values.get("terminal_soc_value_eur", 0) != 0
    ):
        raise ValueError("unapplied terminal valuation must be zero")
    comparison = evidence.comparison
    if comparison is not None:
        if (
            not (comparison.diff_units.keys() | comparison.omitted_objectives.keys())
            <= objective_values.keys()
        ):
            raise ValueError("comparison refers to missing objective values")
        if any(
            evidence.objective_units.get(key) != unit for key, unit in comparison.diff_units.items()
        ):
            raise ValueError("comparison units must match objective units")
    return evidence


def objective_comparison_omission(
    key: str,
    variation: AccountingEvidence | None,
    baseline: AccountingEvidence | None,
    *,
    variation_constraints: dict[str, Any],
    baseline_constraints: dict[str, Any],
) -> OmissionReason | None:
    """One compatibility decision for producers and readers of typed deltas."""

    unit = (variation.objective_units if variation is not None else PHYSICAL_OBJECTIVE_UNITS).get(
        key
    )
    base_unit = (
        baseline.objective_units if baseline is not None else PHYSICAL_OBJECTIVE_UNITS
    ).get(key)
    if key == "terminal_soc_value_eur" and (
        (variation is not None and not variation.terminal_value_applied)
        or (baseline is not None and not baseline.terminal_value_applied)
    ):
        return "not_applicable"
    if key == "solar_self_consumption_kwh" and (
        _solar_accounting_version(variation_constraints)
        != _solar_accounting_version(baseline_constraints)
    ):
        return "accounting_version_mismatch"
    monetary = (
        key in MONETARY_OBJECTIVES
        or key == "terminal_soc_value_eur"
        or any(
            value is not None and value not in {"kWh", "kW", "slot", "1"}
            for value in (unit, base_unit)
        )
    )
    if monetary and (
        variation is None
        or baseline is None
        or variation.currency is None
        or baseline.currency is None
    ):
        return "currency_unknown"
    if (
        monetary
        and variation is not None
        and baseline is not None
        and variation.currency != baseline.currency
    ):
        return "currency_mismatch"
    if unit is None or base_unit is None:
        return "unit_unknown"
    if unit != base_unit:
        return "unit_mismatch"
    return None


def _solar_accounting_version(constraints: dict[str, Any]) -> str | None:
    accounting = constraints.get("solar_accounting")
    convention = accounting.get("convention") if isinstance(accounting, dict) else None
    version = convention.get("accounting_version") if isinstance(convention, dict) else None
    return version if isinstance(version, str) else None


class ProductAlternative(StrictModel):
    plan_id: str = Field(min_length=1, max_length=200)
    objective_values: dict[str, float] = Field(default_factory=dict, max_length=32)
    constraint_effects: dict[str, Any] = Field(default_factory=dict, max_length=16)
    forecast_assumptions: dict[str, Any] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def validate_evidence(self) -> ProductAlternative:
        validate_accounting(self.constraint_effects, self.objective_values)
        return self


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

    @model_validator(mode="after")
    def validate_evidence(self) -> ProductSummary:
        validate_accounting(self.constraint_summary, self.objective_values)
        return self


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


    @model_validator(mode="after")
    def validate_comparison_evidence(self) -> ScenarioComparison:
        base = validate_accounting(self.baseline.constraint_summary, self.baseline.objective_values)
        for variation in self.variations.values():
            evidence = validate_accounting(
                variation.summary.constraint_summary, variation.summary.objective_values
            )
            comparison = evidence.comparison if evidence is not None else None
            if comparison is None:
                continue  # Legacy and partial summaries do not claim a typed comparison.
            if (
                comparison.baseline_scenario_id != self.baseline.scenario_id
                or comparison.baseline_definition_digest != self.baseline.definition_digest
            ):
                raise ValueError("comparison accounting identifies a different baseline")
            if comparison.diff_units.keys() != variation.diff.keys():
                raise ValueError("comparison diff requires matching accounting units")
            for key in variation.diff:
                if key not in self.baseline.objective_values:
                    raise ValueError("comparison delta requires a baseline objective value")
                reason = objective_comparison_omission(
                    key,
                    evidence,
                    base,
                    variation_constraints=variation.summary.constraint_summary,
                    baseline_constraints=self.baseline.constraint_summary,
                )
                if reason is not None:
                    raise ValueError(f"comparison delta is not comparable: {reason}")
        return self


__all__ = [
    "ProductAlternative",
    "ProductSummary",
    "ScenarioComparison",
    "ScenarioComparisonVariation",
]
