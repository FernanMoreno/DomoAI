"""Typed optimizer boundary and solver-neutral result contract."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field

from domoai.domain.models import ErrorDetail, Plan, StrictModel
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


class OptimizationResult(StrictModel):
    schema_version: str = "v1"
    scenario_id: str
    status: OptimizationStatus
    solver: str
    plan: Plan | None = None
    plans: list[Plan] = Field(default_factory=list)
    objective_values: dict[str, float] = Field(default_factory=dict)
    constraint_summary: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[ErrorDetail] = Field(default_factory=list)
    solver_evidence: SolverEvidence | None = None


class OptimizerPort(Protocol):
    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult: ...


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
    solver_evidence: SolverEvidence | None = None,
) -> OptimizationResult:
    return OptimizationResult(
        scenario_id=scenario_id,
        status=status,
        solver="cp-sat",
        plan=plan,
        plans=plans or [],
        diagnostics=[ErrorDetail.model_validate(item) for item in (diagnostics or [])],
        objective_values=objective_values or {},
        constraint_summary=constraint_summary or {},
        solver_evidence=solver_evidence,
    )
