"""Optimization orchestration that returns proposals, never adapter calls."""

from __future__ import annotations

from domoai.application.plan_service import PlanService
from domoai.domain.models import Plan
from domoai.optimizer.ports import OptimizationResult, OptimizerPort
from domoai.optimizer.scenario import OptimizationScenario
from domoai.runtime.registry import DeviceRegistry


class OptimizationService:
    def __init__(
        self,
        registry: DeviceRegistry,
        plan_service: PlanService,
        optimizer: OptimizerPort,
    ) -> None:
        self.registry = registry
        self.plan_service = plan_service
        self.optimizer = optimizer
        self._last_wall_time_seconds: float | None = None

    @property
    def last_wall_time_seconds(self) -> float | None:
        return self._last_wall_time_seconds

    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        result = self.optimizer.optimize(scenario)
        if result.solver_evidence is not None:
            self._last_wall_time_seconds = result.solver_evidence.wall_time_seconds
        return result

    def validate_proposal(self, result: OptimizationResult) -> OptimizationResult:
        bundle = result.plans or ([result.plan] if result.plan is not None else [])
        if not bundle:
            return result
        validated: list[Plan] = [self.plan_service.validate(plan) for plan in bundle]
        return result.model_copy(update={"plan": validated[0], "plans": validated})
