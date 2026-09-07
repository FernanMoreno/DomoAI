import pytest

from domoai.domain.models import Command, Plan
from domoai.mcp.ortools_server import explain_result
from domoai.optimizer.ports import OptimizationStatus, build_result


def test_explanation_contains_proposal_and_hard_constraint_evidence() -> None:
    result = build_result(
        scenario_id="energy-001",
        status=OptimizationStatus.FEASIBLE,
        plan=Plan(
            id="proposal-energy-001",
            commands=[
                Command(
                    id="proposal-command-001",
                    device_id="living_room.main_light",
                    command="turn_on",
                    idempotency_key="proposal-intent-001",
                )
            ],
        ),
        objective_values={"start_slot_sum": 3.0},
        constraint_summary={"hard_satisfied": True, "soft_violations": []},
    )

    explanation = explain_result(result)

    assert explanation.status is OptimizationStatus.FEASIBLE
    assert explanation.proposal is not None
    assert explanation.proposal["plan_id"] == "proposal-energy-001"
    assert explanation.constraint_summary["hard_satisfied"] is True
    assert "feasible" in explanation.summary.lower()
    assert explanation.next_step == "validate_and_request_approval_before_execution"
    assert explanation.proposal_count == 1


def test_explanation_preserves_diagnostics_without_inventing_a_proposal() -> None:
    result = build_result(
        scenario_id="energy-002",
        status=OptimizationStatus.INFEASIBLE,
        diagnostics=[{"code": "infeasible", "message": "Power limit cannot fit the load"}],
    )

    explanation = explain_result(result)

    assert explanation.proposal is None
    assert explanation.diagnostics[0].code == "infeasible"
    assert "infeasible" in explanation.summary.lower()
    assert explanation.next_step == "revise_scenario_or_inputs"
    assert explanation.proposal_count == 0


def test_explanation_projects_bounded_forecast_and_alternative_evidence() -> None:
    primary = Plan(
        id="proposal-primary",
        commands=[
            Command(
                id="command-primary",
                device_id="living_room.main_light",
                command="turn_on",
                idempotency_key="intent-primary",
            )
        ],
    )
    alternative = primary.model_copy(update={"id": "proposal-alternative"})
    result = build_result(
        scenario_id="energy-explainable",
        status=OptimizationStatus.FEASIBLE,
        plan=primary,
        plans=[primary, alternative],
        alternative_evidence={
            "proposal-primary": {
                "objective_values": {"energy_cost": 1.2, "comfort_score": 0.9},
                "constraint_effects": {"hard_satisfied": True, "comfort_penalty": 0.0},
                "forecast_assumptions": {"confidence": "medium", "conservative": False},
            },
            "proposal-alternative": {
                "objective_values": {"energy_cost": 1.6, "comfort_score": 1.0},
                "constraint_effects": {"hard_satisfied": True, "comfort_penalty": 0.2},
                "forecast_assumptions": {"confidence": "medium", "conservative": False},
            },
        },
        constraint_summary={
            "hard_satisfied": True,
            "soft_violations": [{"type": "comfort", "amount": 0.1}],
            "forecast_confidence": "medium",
        },
    )

    explanation = explain_result(result)

    assert explanation.forecast_confidence == "medium"
    assert explanation.alternatives == [
        {
            "plan_id": "proposal-primary",
            "status": "draft",
            "objective_values": {"energy_cost": 1.2, "comfort_score": 0.9},
            "constraint_effects": {"hard_satisfied": True, "comfort_penalty": 0.0},
            "forecast_assumptions": {"confidence": "medium", "conservative": False},
        },
        {
            "plan_id": "proposal-alternative",
            "status": "draft",
            "objective_values": {"energy_cost": 1.6, "comfort_score": 1.0},
            "constraint_effects": {"hard_satisfied": True, "comfort_penalty": 0.2},
            "forecast_assumptions": {"confidence": "medium", "conservative": False},
        },
    ]
    assert explanation.hard_constraints_satisfied is True
    assert explanation.soft_violations == [{"type": "comfort", "amount": 0.1}]


@pytest.mark.parametrize(
    "status",
    [OptimizationStatus.OPTIMAL, OptimizationStatus.INVALID, OptimizationStatus.TIMEOUT],
)
def test_explanation_covers_remaining_solver_statuses(status: OptimizationStatus) -> None:
    result = build_result(
        scenario_id=f"energy-{status.value}",
        status=status,
        plan=(
            Plan(
                id=f"proposal-{status.value}",
                commands=[
                    Command(
                        id=f"command-{status.value}",
                        device_id="living_room.main_light",
                        command="turn_on",
                        idempotency_key=f"intent-{status.value}",
                    )
                ],
            )
            if status is OptimizationStatus.OPTIMAL
            else None
        ),
        diagnostics=(
            []
            if status is OptimizationStatus.OPTIMAL
            else [{"code": status.value, "message": f"Scenario is {status.value}"}]
        ),
    )

    explanation = explain_result(result)

    assert explanation.status is status
    if status is OptimizationStatus.OPTIMAL:
        assert explanation.proposal is not None
    else:
        assert explanation.proposal is None
        assert explanation.diagnostics[0].code == status.value
