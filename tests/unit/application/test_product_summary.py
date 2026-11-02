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


def test_unknown_base_limits_public_hard_constraint_claim_and_preserves_assumptions() -> None:
    result = _result()
    result.constraint_summary["forecast_assumptions"] = {
        "constraint_scope": "modeled_inputs",
        "base_load": {
            "status": "unknown",
            "kind": "unknown",
            "coverage": "missing",
            "all_zero": None,
            "household_limit_guaranteed": False,
        },
    }
    summary = build_product_summary(result)
    assert summary.hard_constraints_satisfied is None
    assert "unknown" in summary.headline.lower()
    assert (
        summary.constraint_summary["forecast_assumptions"]
        == result.constraint_summary["forecast_assumptions"]
    )
    assert result.constraint_summary["hard_satisfied"] is True


def test_solar_accounting_is_preserved_in_passive_product_summary() -> None:
    result = build_result(
        scenario_id="solar-product",
        status=OptimizationStatus.NO_ACTION_REQUIRED,
        objective_values={"solar_self_consumption_kwh": 1},
        constraint_summary={"solar_accounting": {
            "scope": "horizon",
            "self_consumption_kwh": 1,
            "convention": {"evidence_kind": "modeled_attribution", "unit": "kWh"},
        }},
    )
    summary = build_product_summary(result)
    assert summary.constraint_summary["solar_accounting"] == (
        result.constraint_summary["solar_accounting"]
    )
    assert summary.proposal_id is None


def test_a12_product_keeps_alternative_currency_and_forecast_evidence():
    from tests.contract.test_phase4_product_contract import _a12_accounting

    result = _result()
    result.constraint_summary["accounting"] = {
        **_a12_accounting(),
        "objective_units": {"energy_cost": "EUR"},
    }
    result.alternative_evidence["proposal-alternative"].constraint_effects["accounting"] = {
        **_a12_accounting("USD"),
        "objective_units": {"energy_cost": "USD"},
    }
    summary = build_product_summary(result)
    assert summary.constraint_summary["accounting"]["currency"] == "EUR"
    assert summary.alternatives[0].constraint_effects["accounting"]["currency"] == "USD"
    assert "accounting" not in summary.alternatives[1].constraint_effects
    assert summary.alternatives[0].forecast_assumptions == {"confidence": "medium"}
