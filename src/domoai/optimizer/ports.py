"""Typed optimizer boundary and solver-neutral result contract."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field, model_validator

from domoai.domain.models import ErrorDetail, Plan, StrictModel
from domoai.domain.product import validate_accounting
from domoai.optimizer.energy import EnergyContext
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.scenario import OptimizationScenario


class OptimizationStatus(StrEnum):
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    OPTIMAL_HIERARCHY = "optimal_hierarchy"
    FEASIBLE_HIERARCHY = "feasible_hierarchy"
    NO_ACTION_REQUIRED = "no_action_required"
    INFEASIBLE = "infeasible"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class SolvedTier(StrictModel):
    """One priority tier actually solved for a result.

    `priority` is `None` for the implicit soft-constraint-violation tier,
    which has no user-declared priority and always precedes every declared
    tier when it is present.
    """

    priority: int | None
    terms: list[str]
    achieved_value: int


class SolverEvidence(StrictModel):
    """Audit trail for one completed solve; see spec 028."""

    solver_name: str
    solver_version: str
    num_search_workers: int
    random_seed: int
    wall_time_seconds: float
    tiers: list[SolvedTier]
    scenario_fingerprint: str


class AlternativeEvidence(StrictModel):
    """Comparable, bounded evidence for one proposal in a result bundle."""

    objective_values: dict[str, float] = Field(default_factory=dict, max_length=32)
    constraint_effects: dict[str, Any] = Field(default_factory=dict, max_length=16)
    forecast_assumptions: dict[str, Any] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def validate_evidence(self) -> AlternativeEvidence:
        validate_accounting(self.constraint_effects, self.objective_values)
        return self


class OptimizationResult(StrictModel):
    schema_version: str = "v1"
    scenario_id: str
    definition_digest: str | None = None
    status: OptimizationStatus
    solver: str
    plan: Plan | None = None
    plans: list[Plan] = Field(default_factory=list)
    objective_values: dict[str, float] = Field(default_factory=dict)
    constraint_summary: dict[str, Any] = Field(default_factory=dict)
    alternative_evidence: dict[str, AlternativeEvidence] = Field(default_factory=dict)
    diagnostics: list[ErrorDetail] = Field(default_factory=list)
    solver_evidence: SolverEvidence | None = None

    @model_validator(mode="before")
    @classmethod
    def select_contract_version(cls, value: Any) -> Any:
        if isinstance(value, dict) and "schema_version" not in value:
            plans = [Plan.model_validate(item) for item in value.get("plans", [])]
            plan = Plan.model_validate(value["plan"]) if value.get("plan") is not None else None
            version = (
                "v2"
                if any(item.schema_version == "v2" for item in [*plans, *([plan] if plan else [])])
                else "v1"
            )
            return {**value, "plans": plans, "plan": plan, "schema_version": version}
        return value

    @model_validator(mode="after")
    def validate_contract_version(self) -> OptimizationResult:
        validate_accounting(self.constraint_summary, self.objective_values)
        version = (
            "v2"
            if any(
                item.schema_version == "v2"
                for item in [*self.plans, *([self.plan] if self.plan else [])]
            )
            else "v1"
        )
        if self.schema_version != version:
            raise ValueError("optimization result schema_version does not match its plans")
        return self


class OptimizerPort(Protocol):
    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult: ...


class BoundedOptimizerWorkerPort(Protocol):
    """Async worker boundary both `OptimizationWorker` (thread-backed) and
    `ProcessOptimizationWorker` (spec 150, process-backed) satisfy --
    referenced structurally so callers like `OrtoolsMcpContext` don't need
    to import either concrete worker class."""

    async def optimize(self, scenario: OptimizationScenario) -> OptimizationResult: ...


class EnergyContextProvider(Protocol):
    """Read-only semantic provider for one complete optimization horizon."""

    def get_context(self, horizon: Horizon) -> EnergyContext: ...


def build_result(
    *,
    scenario_id: str,
    status: OptimizationStatus,
    plan: Plan | None = None,
    plans: list[Plan] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    objective_values: dict[str, float] | None = None,
    constraint_summary: dict[str, Any] | None = None,
    alternative_evidence: dict[str, AlternativeEvidence | dict[str, Any]] | None = None,
    solver_evidence: SolverEvidence | None = None,
    definition_digest: str | None = None,
) -> OptimizationResult:
    normalized_alternative_evidence = {
        plan_id: (
            evidence
            if isinstance(evidence, AlternativeEvidence)
            else AlternativeEvidence.model_validate(evidence)
        )
        for plan_id, evidence in (alternative_evidence or {}).items()
    }
    return OptimizationResult(
        scenario_id=scenario_id,
        definition_digest=definition_digest,
        status=status,
        solver="cp-sat",
        plan=plan,
        plans=plans or [],
        diagnostics=[ErrorDetail.model_validate(item) for item in (diagnostics or [])],
        objective_values=objective_values or {},
        constraint_summary=constraint_summary or {},
        alternative_evidence=normalized_alternative_evidence,
        solver_evidence=solver_evidence,
    )
