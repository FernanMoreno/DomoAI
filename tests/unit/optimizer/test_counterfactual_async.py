import pytest

from domoai.optimizer.counterfactual import CounterfactualAnalyzer
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import Horizon, OptimizationScenario


def _scenario(identifier: str) -> OptimizationScenario:
    from datetime import UTC, datetime

    return OptimizationScenario(
        id=identifier,
        horizon=Horizon(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 1, 1, tzinfo=UTC),
            resolution_minutes=15,
            timezone="Europe/Madrid",
        ),
    )


class _Worker:
    async def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        return OptimizationResult(
            scenario_id=scenario.id,
            definition_digest=f"digest:{scenario.id}",
            status=(
                OptimizationStatus.INFEASIBLE
                if scenario.id == "bad"
                else OptimizationStatus.FEASIBLE
            ),
            solver="test",
            objective_values={"cost": 1.0 if scenario.id == "base" else 2.0},
        )


@pytest.mark.asyncio
async def test_async_counterfactual_reuses_bounded_worker_and_skips_after_bad_baseline() -> None:
    analyzer = CounterfactualAnalyzer(_Worker())

    result = await analyzer.compare_async(
        _scenario("base"), {"bad": _scenario("bad")},
    )

    assert result.baseline.status is OptimizationStatus.FEASIBLE
    assert result.variations["bad"].diff == {}

    stopped = await analyzer.compare_async(
        _scenario("bad"), {"never_run": _scenario("variation")},
    )
    assert stopped.baseline.status is OptimizationStatus.INFEASIBLE
    assert stopped.variations == {}
