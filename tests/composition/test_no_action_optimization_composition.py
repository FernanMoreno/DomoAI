from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.mcp.ortools_server import explain_result
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Horizon, OptimizationScenario
from domoai.runtime.registry import DeviceRegistry


@pytest.mark.composition
def test_valid_zero_transition_optimization_is_successful_and_non_executable() -> None:
    start = datetime(2026, 8, 23, 12, tzinfo=UTC)
    scenario = OptimizationScenario(
        id="already-optimal",
        horizon=Horizon(
            start=start,
            end=start + timedelta(hours=1),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        ),
    )

    result = CpSatOptimizer(DeviceRegistry()).optimize(scenario)
    explanation = explain_result(result)

    assert result.status is OptimizationStatus.NO_ACTION_REQUIRED
    assert result.plan is None
    assert result.plans == []
    assert "no physical action" in explanation.summary
    assert explanation.proposal is None
