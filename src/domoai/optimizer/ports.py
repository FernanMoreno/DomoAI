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
    INFEASIBLE = "infeasible"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class OptimizationResult(StrictModel):
    schema_version: str = "v1"
    scenario_id: str
    status: OptimizationStatus
    solver: str
    plan: Plan | None = None
    objective_values: dict[str, float] = Field(default_factory=dict)
    constraint_summary: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[ErrorDetail] = Field(default_factory=list)


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
    diagnostics: list[dict[str, Any]] | None = None,
    objective_values: dict[str, float] | None = None,
    constraint_summary: dict[str, Any] | None = None,
) -> OptimizationResult:
    return OptimizationResult(
        scenario_id=scenario_id,
        status=status,
        solver="cp-sat",
        plan=plan,
        diagnostics=[ErrorDetail.model_validate(item) for item in (diagnostics or [])],
        objective_values=objective_values or {},
        constraint_summary=constraint_summary or {},
    )
