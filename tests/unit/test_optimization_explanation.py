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


def test_explanation_preserves_diagnostics_without_inventing_a_proposal() -> None:
    result = build_result(
        scenario_id="energy-002",
        status=OptimizationStatus.INFEASIBLE,
        diagnostics=[
            {"code": "infeasible", "message": "Power limit cannot fit the load"}
        ],
    )

    explanation = explain_result(result)

    assert explanation.proposal is None
    assert explanation.diagnostics[0].code == "infeasible"
    assert "infeasible" in explanation.summary.lower()


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
