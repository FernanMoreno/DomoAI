from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domoai.optimizer.ports import OptimizationStatus, build_result
from domoai.optimizer.scenario import (
    Constraint,
    Horizon,
    Load,
    Objective,
    OptimizationScenario,
    validate_scenario,
)
from domoai.runtime.registry import DeviceRegistry


def test_scenario_contract_serializes_versioned_horizon_and_objectives() -> None:
    scenario = OptimizationScenario(
        id="scenario-contract-1",
        horizon=Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 4, tzinfo=UTC),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        ),
        loads=[
            Load(
                id="load-1",
                device_id="garage.ev",
                capability="power",
                command="turn_on",
                power=3.6,
                power_unit="kW",
            )
        ],
        constraints=[Constraint(type="max_house_power", value=5750, unit="W")],
        objectives=[Objective(name="minimize_start", direction="minimize", weight=1)],
    )

    payload = scenario.model_dump(mode="json")

    assert payload["schema_version"] == "v1"
    assert payload["horizon"]["resolution_minutes"] == 60
    assert payload["objectives"][0]["direction"] == "minimize"


def test_scenario_rejects_invalid_horizon_and_non_positive_resolution() -> None:
    with pytest.raises(ValidationError):
        Horizon(
            start=datetime(2026, 8, 15, 4, tzinfo=UTC),
            end=datetime(2026, 8, 15, tzinfo=UTC),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        )
    with pytest.raises(ValidationError):
        Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 1, 30, tzinfo=UTC),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        )
    with pytest.raises(ValidationError):
        Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 1, tzinfo=UTC),
            resolution_minutes=0,
            timezone="Europe/Madrid",
        )


def test_optimization_result_statuses_are_typed_and_diagnostics_are_structured() -> None:
    result = build_result(
        scenario_id="scenario-contract-2",
        status=OptimizationStatus.INFEASIBLE,
        diagnostics=[{"code": "infeasible", "message": "Power limit is too low"}],
    )

    assert result.status is OptimizationStatus.INFEASIBLE
    assert result.plan is None
    assert result.diagnostics[0].code == "infeasible"


def test_scenario_rejects_vendor_adapter_and_solver_code_inputs() -> None:
    scenario = OptimizationScenario(
        id="scenario-semantic-only",
        horizon=Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 1, tzinfo=UTC),
            resolution_minutes=60,
            timezone="Europe/Madrid",
        ),
        inputs=[{"adapter_id": "vendor-api", "code": "execute_python(...)"}],
    )

    diagnostics = validate_scenario(scenario, DeviceRegistry())

    assert [item.code for item in diagnostics] == ["non_semantic_input"]
