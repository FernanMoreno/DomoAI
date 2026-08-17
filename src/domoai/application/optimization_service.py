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

    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        return self.optimizer.optimize(scenario)

    def validate_proposal(self, result: OptimizationResult) -> OptimizationResult:
        if result.plan is None:
            return result
        validated: Plan = self.plan_service.validate(result.plan)
        return result.model_copy(update={"plan": validated})
