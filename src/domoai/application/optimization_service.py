"""Optimization orchestration that returns proposals, never adapter calls."""

from __future__ import annotations

from domoai.application.plan_service import PlanService
from domoai.domain.models import ErrorDetail, Plan
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus, OptimizerPort
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
        if (
            result.status
            in {
                OptimizationStatus.OPTIMAL,
                OptimizationStatus.FEASIBLE,
                OptimizationStatus.OPTIMAL_HIERARCHY,
                OptimizationStatus.FEASIBLE_HIERARCHY,
            }
            and self._has_unbound_battery_dispatch(result)
        ):
            return result.model_copy(
                update={
                    "status": OptimizationStatus.INVALID,
                    "plan": None,
                    "plans": [],
                    "diagnostics": [
                        *result.diagnostics,
                        ErrorDetail(
                            code="battery_actuation_unbound",
                            message="Battery dispatch has no physical actuator binding",
                            retryable=False,
                        ),
                    ],
                }
            )
        bundle = result.plans or ([result.plan] if result.plan is not None else [])
        if not bundle:
            return result
        validated: list[Plan] = [self.plan_service.validate(plan) for plan in bundle]
        return result.model_copy(update={"plan": validated[0], "plans": validated})

    @staticmethod
    def _has_unbound_battery_dispatch(result: OptimizationResult) -> bool:
        if result.constraint_summary.get("battery_actuator_bound") is True:
            return False
        slots = result.constraint_summary.get("slots")
        if not isinstance(slots, list):
            return False
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            for field in ("battery_charge_kw", "battery_discharge_kw"):
                value = slot.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    return True
        return False
