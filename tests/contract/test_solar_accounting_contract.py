"""Accounting evidence survives serialization and rejects false energy claims."""

import copy

import pytest
from pydantic import ValidationError

from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.ports import OptimizationResult
from domoai.runtime.registry import DeviceRegistry
from tests.integration.test_energy_optimization import solar_accounting_scenario


def accounting_payload():
    result = CpSatOptimizer(DeviceRegistry()).optimize(solar_accounting_scenario())
    return result, result.constraint_summary["solar_accounting"]


def test_accounting_evidence_is_typed_and_roundtrips_in_existing_envelope() -> None:
    result, payload = accounting_payload()
    from domoai.optimizer.solar_accounting import SolarAccountingEvidence

    evidence = SolarAccountingEvidence.model_validate(payload)
    assert evidence.convention.evidence_kind == "modeled_attribution"
    assert evidence.convention.unit == "kWh"
    assert evidence.self_consumption_kwh == pytest.approx(1)
    assert [r.origin for r in evidence.battery_origins] == [
        "horizon_solar", "horizon_grid", "initial_unknown",
    ]
    assert evidence.battery_origins[2].exported_kwh == pytest.approx(1)
    assert OptimizationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("corruption", [
    "negative", "nan", "infinity", "wrong_unit", "wrong_version", "duplicate_origin",
    "missing_origin", "false_consumption", "false_loss", "solar_initial", "unknown_charge",
    "wrong_scales", "extra_field", "excess_credit",
])
def test_accounting_rejects_corrupt_origin_energy_and_units(corruption: str) -> None:
    _, original = accounting_payload()
    from domoai.optimizer.solar_accounting import SolarAccountingEvidence

    payload = copy.deepcopy(original)
    if corruption in {"negative", "nan", "infinity"}:
        payload["generated_kwh"] = {"negative": -1, "nan": float("nan"),
                                    "infinity": float("inf")}[corruption]
    elif corruption == "wrong_unit":
        payload["convention"]["unit"] = "kW"
    elif corruption == "wrong_version":
        payload["convention"]["accounting_version"] = "measured"
    elif corruption == "duplicate_origin":
        payload["battery_origins"][1]["origin"] = "horizon_solar"
    elif corruption == "missing_origin":
        payload["battery_origins"].pop()
    elif corruption == "false_consumption":
        payload["self_consumption_kwh"] = 2
    elif corruption == "false_loss":
        payload["battery_origins"][2]["discharge_loss_kwh"] = 1
    elif corruption == "solar_initial":
        payload["battery_origins"][0]["initial_kwh"] = 1
        payload["battery_origins"][0]["terminal_stored_kwh"] = 1
    elif corruption == "unknown_charge":
        payload["battery_origins"][2]["charged_kwh"] = 1
        payload["battery_origins"][2]["terminal_stored_kwh"] = 1
    elif corruption == "wrong_scales":
        payload["convention"]["soc_scale"] = 100
    elif corruption == "extra_field":
        payload["measured_origin"] = True
    elif corruption == "excess_credit":
        payload["objective_credit_kwh"] = 2
    with pytest.raises(ValidationError):
        SolarAccountingEvidence.model_validate(payload)


@pytest.mark.parametrize("left,right,comparable", [
    ("solar-v1", "solar-v1", True), (None, None, True),
    ("solar-v1", None, False), (None, "solar-v1", False),
    ("solar-v1", "solar-v2", False),
])
def test_comparison_does_not_subtract_incompatible_solar_definitions(left, right, comparable):
    from domoai.optimizer.counterfactual import _diff
    from domoai.optimizer.ports import OptimizationStatus, build_result

    def result(version, solar, cost):
        return build_result(
            scenario_id="solar-version-comparison", status=OptimizationStatus.NO_ACTION_REQUIRED,
            objective_values={"solar_self_consumption_kwh": solar, "energy_cost": cost},
            constraint_summary={
                "accounting": {
                    "version": 1, "currency": "EUR",
                    "objective_units": {
                        "energy_cost": "EUR", "solar_self_consumption_kwh": "kWh",
                    },
                },
                **({"solar_accounting": {
                    "convention": {"accounting_version": version},
                }} if version is not None else {}),
            },
        )

    difference = _diff(result(left, 2, 3), result(right, 1, 2))
    assert difference["energy_cost"] == 1
    assert ("solar_self_consumption_kwh" in difference) is comparable
    if comparable:
        assert difference["solar_self_consumption_kwh"] == 1
