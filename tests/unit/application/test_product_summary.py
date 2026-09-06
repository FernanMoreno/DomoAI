from domoai.domain.models import Command, Plan
from domoai.optimizer.ports import OptimizationStatus, build_result
from domoai.optimizer.product import build_product_summary


def _result():
    primary = Plan(
        id="proposal-primary",
        commands=[
            Command(
                id="command-primary",
                device_id="light.living_room_main",
                command="turn_on",
                idempotency_key="intent-primary",
            )
        ],
    )
    alternative = primary.model_copy(update={"id": "proposal-alternative"})
    return build_result(
        scenario_id="energy-product-001",
        status=OptimizationStatus.FEASIBLE,
        plan=primary,
        plans=[primary, alternative],
        objective_values={"energy_cost": 1.2, "comfort_score": 0.9},
        constraint_summary={
            "hard_satisfied": True,
            "forecast_confidence": "medium",
            "soft_violations": [],
        },
        alternative_evidence={
            "proposal-primary": {
                "objective_values": {"energy_cost": 1.2},
                "constraint_effects": {"comfort_penalty": 0.0},
                "forecast_assumptions": {"confidence": "medium"},
            },
            "proposal-alternative": {
                "objective_values": {"energy_cost": 1.5},
                "constraint_effects": {"comfort_penalty": 0.2},
                "forecast_assumptions": {"confidence": "medium"},
            },
        },
    )


def test_product_summary_is_deterministic_and_read_only() -> None:
    first = build_product_summary(_result())
    second = build_product_summary(_result())

    assert first == second
    assert first.scenario_id == "energy-product-001"
    assert first.hard_constraints_satisfied is True
    assert [item.plan_id for item in first.alternatives] == [
        "proposal-alternative",
        "proposal-primary",
    ]
    assert first.next_step == "validate_and_request_approval_before_execution"
    assert "commands" not in first.model_dump(mode="json")


def test_product_summary_does_not_invent_proposal_for_infeasible_result() -> None:
    result = build_result(
        scenario_id="energy-product-invalid",
        status=OptimizationStatus.INFEASIBLE,
        diagnostics=[{"code": "infeasible", "message": "constraints conflict"}],
    )

    summary = build_product_summary(result)

    assert summary.proposal_id is None
    assert summary.alternatives == []
    assert summary.next_step == "revise_scenario_or_inputs"
