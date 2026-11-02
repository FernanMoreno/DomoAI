import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.adapters.sdk import AdapterManifest
from domoai.domain.commissioning import CommissioningQualification
from domoai.domain.models import AuthorityContext
from domoai.domain.privacy import HouseholdDataPolicy, PrivacyCategory
from domoai.domain.product import ProductSummary

SCHEMA_DIR = Path(__file__).parents[2] / "schemas" / "v1"


def test_phase4_public_schemas_are_exported_and_versioned() -> None:
    for name in (
        "commissioning-evidence",
        "commissioning-qualification",
        "commissioning-report",
        "household-data-policy",
        "privacy-export",
        "privacy-deletion",
        "product-summary",
    ):
        payload = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert payload["properties"]["schema_version"]["const"] == "v1"

    for name in ("capability", "commissioning-check"):
        payload = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert payload["type"] == "object"
        assert payload["additionalProperties"] is False


def test_provider_manifest_carries_the_universal_guarantee_contract() -> None:
    manifest = AdapterManifest(
        adapter_id="fixture",
        name="Fixture",
        protocol="fixture",
        package_name="domoai-tests",
        package_version="1.0.0",
        device_types=["light"],
        capabilities=[
            {
                "name": "brightness",
                "kind": "integer",
                "unit": "%",
                "readable": True,
                "writable": True,
                "minimum": 0,
                "maximum": 100,
                "commands": ["set_brightness"],
                "guarantees": {
                    "resolution": 1,
                    "readback_required": True,
                },
            }
        ],
    )

    assert manifest.capabilities[0].guarantees.readback_required is True


def test_phase4_contract_models_are_strict_and_secret_free() -> None:
    authority = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="owner-a",
        roles=["owner"],
    )
    qualification = CommissioningQualification(
        authority=authority,
        candidate_digest="a" * 64,
        evidence_digest="sha256:" + "b" * 64,
        status="blocked_external_dependency",
        blockers=["physical_evidence_required"],
        checked_at=datetime.now(UTC),
    )
    policy = HouseholdDataPolicy(
        authority=authority,
        exportable_categories=[PrivacyCategory.PLANS],
        deletable_categories=[PrivacyCategory.PLANS],
        retention_days=90,
    )
    summary = ProductSummary(
        scenario_id="scenario-1",
        status="feasible",
        headline="Proposal",
        next_step="validate_and_request_approval_before_execution",
    )

    serialized = json.dumps(
        {
            "qualification": qualification.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
            "summary": summary.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    assert "token" not in serialized.casefold()
    assert "secret" not in serialized.casefold()


# Accounting is validated inside existing extensible dictionaries, not envelope fields.
def _a12_result_payload(accounting):
    return {
        "scenario_id": "accounting",
        "status": "no_action_required",
        "solver": "cp-sat",
        "objective_values": {
            "energy_cost": 0.4,
            "peak_import_kw": 1.0,
            "terminal_soc_value_eur": 0.0,
        },
        "constraint_summary": {"accounting": accounting},
    }


def _a12_accounting(currency="EUR"):
    return {
        "version": 1,
        "currency": currency,
        "source_revision_digest": "a" * 64,
        "objective_units": {
            "energy_cost": currency,
            "peak_import_kw": "kW",
            "terminal_soc_value_eur": "EUR",
        },
        "degradation_currency_source": "not_configured",
        "terminal_value_applied": False,
    }


def test_a12_accounting_readers_reject_contradictory_or_corrupt_metadata():
    import pytest
    from pydantic import ValidationError

    from domoai.domain.product import ProductAlternative
    from domoai.mcp.ortools_server import OptimizationExplanation
    from domoai.optimizer.ports import AlternativeEvidence, OptimizationResult

    for change in (
        {"version": 2},
        {"currency": "eur"},
        {"currency": "USD"},
        {"source_revision_digest": "short"},
        {"unknown": True},
        {"objective_units": {"missing": "kWh"}},
        {"objective_units": {"peak_import_kw": "EUR"}},
        {"objective_units": {"energy_cost": "kWh"}},
        {"terminal_value_applied": "false"},
        {"objective_units": {"energy_cost": "eur"}},
        {"objective_units": {"x" * 65: "kW"}},
    ):
        payload = _a12_result_payload({**_a12_accounting(), **change})
        values = payload["objective_values"]
        effects = payload["constraint_summary"]
        for model, candidate in (
            (OptimizationResult, payload),
            (AlternativeEvidence, {"objective_values": values, "constraint_effects": effects}),
            (
                ProductAlternative,
                {"plan_id": "p", "objective_values": values, "constraint_effects": effects},
            ),
            (
                ProductSummary,
                {
                    "scenario_id": "s",
                    "status": "no_action_required",
                    "headline": "Ready",
                    "next_step": "no_action_required",
                    "objective_values": values,
                    "constraint_summary": effects,
                },
            ),
            (OptimizationExplanation, {**payload, "summary": "Ready", "diagnostics": []}),
        ):
            with pytest.raises(ValidationError):
                model.model_validate(candidate)


def test_a12_accounting_partial_roundtrip_and_envelope_stay_compatible():
    from domoai.optimizer.ports import OptimizationResult, build_result
    from domoai.optimizer.product import build_product_summary

    accounting = {**_a12_accounting("USD"), "objective_units": {"energy_cost": "USD"}}
    result = OptimizationResult.model_validate(_a12_result_payload(accounting))
    rebuilt = build_result(
        scenario_id=result.scenario_id,
        status=result.status,
        objective_values=result.objective_values,
        constraint_summary=result.constraint_summary,
    )
    assert OptimizationResult.model_validate_json(rebuilt.model_dump_json()) == result
    assert result.schema_version == "v1"
    assert build_product_summary(result).constraint_summary["accounting"] == accounting


def test_a12_accounting_rejects_null_block_and_nonapplied_nonzero_terminal():
    import pytest
    from pydantic import ValidationError

    from domoai.optimizer.ports import OptimizationResult

    for block in (None, {**_a12_accounting("USD"), "terminal_value_applied": True}):
        with pytest.raises(ValidationError):
            OptimizationResult.model_validate(_a12_result_payload(block))
    payload = _a12_result_payload(_a12_accounting())
    payload["objective_values"]["terminal_soc_value_eur"] = 1
    with pytest.raises(ValidationError):
        OptimizationResult.model_validate(payload)


def test_a12_accounting_limits_and_strict_version():
    import pytest
    from pydantic import ValidationError

    from domoai.domain.product import AccountingEvidence, ComparisonAccounting
    from domoai.optimizer.ports import AlternativeEvidence

    with pytest.raises(ValidationError):
        AccountingEvidence.model_validate({"version": True})
    with pytest.raises(ValidationError):
        AccountingEvidence.model_validate({"objective_units": {f"m{i}": "kW" for i in range(33)}})
    with pytest.raises(ValidationError):
        ComparisonAccounting.model_validate(
            {
                "baseline_scenario_id": "b",
                "diff_units": {"m": "kW"},
                "omitted_objectives": {"m": "unit_unknown"},
            }
        )
    with pytest.raises(ValidationError):
        AlternativeEvidence(
            constraint_effects={**{f"c{i}": True for i in range(16)}, "accounting": {"version": 1}}
        )
    assert (
        len(
            AlternativeEvidence(
                constraint_effects={
                    **{f"c{i}": True for i in range(15)},
                    "accounting": {"version": 1},
                }
            ).constraint_effects
        )
        == 16
    )


def test_a12_partial_monetary_unit_does_not_invent_currency():
    from domoai.optimizer.ports import OptimizationResult
    payload = _a12_result_payload({**_a12_accounting(), "currency": None})
    result = OptimizationResult.model_validate(payload)
    assert result.constraint_summary["accounting"]["currency"] is None
    assert result.constraint_summary["accounting"]["objective_units"]["energy_cost"] == "EUR"
    assert OptimizationResult.model_validate_json(result.model_dump_json()) == result


def test_a12_public_comparison_reader_checks_units_and_baseline_identity():
    from copy import deepcopy

    import pytest
    from pydantic import ValidationError

    from domoai.domain.product import ScenarioComparison
    from domoai.optimizer.ports import OptimizationResult
    from domoai.optimizer.product import build_product_summary

    baseline = build_product_summary(
        OptimizationResult.model_validate(_a12_result_payload(_a12_accounting()))
    ).model_dump(mode="json")
    variation = deepcopy(baseline)
    variation["scenario_id"] = "variation"
    variation["constraint_summary"]["accounting"]["comparison"] = {
        "baseline_scenario_id": baseline["scenario_id"],
        "baseline_definition_digest": baseline["definition_digest"],
        "diff_units": {"energy_cost": "EUR"},
        "omitted_objectives": {},
    }
    payload = {
        "runtime_revision": "r1",
        "baseline": baseline,
        "variations": {
            "v": {
                "scenario_id": "variation",
                "status": "no_action_required",
                "summary": variation,
                "diff": {"energy_cost": 0},
            }
        },
    }
    assert ScenarioComparison.model_validate(payload).variations["v"].diff == {"energy_cost": 0}
    for corruption in ("baseline", "digest", "units"):
        invalid = deepcopy(payload)
        block = invalid["variations"]["v"]["summary"]["constraint_summary"]["accounting"][
            "comparison"
        ]
        if corruption == "baseline":
            block["baseline_scenario_id"] = "other"
        elif corruption == "digest":
            block["baseline_definition_digest"] = "other-digest"
        else:
            block["diff_units"] = {}
        with pytest.raises(ValidationError):
            ScenarioComparison.model_validate(invalid)


def _f01_comparison_payload(currency="EUR"):
    from copy import deepcopy

    baseline = {
        "scenario_id": "base",
        "definition_digest": "base-definition",
        "status": "no_action_required",
        "headline": "No action",
        "next_step": "no_action_required",
        "objective_values": {"energy_cost": 2.0, "peak_import_kw": 1.0},
        "constraint_summary": {
            "accounting": {
                "version": 1,
                "currency": currency,
                "objective_units": {"energy_cost": currency, "peak_import_kw": "kW"},
                "terminal_value_applied": False,
            }
        },
    }
    summary = deepcopy(baseline)
    summary["scenario_id"] = "variation"
    summary["objective_values"]["energy_cost"] = 3.0
    summary["constraint_summary"]["accounting"]["comparison"] = {
        "baseline_scenario_id": "base",
        "baseline_definition_digest": "base-definition",
        "diff_units": {"energy_cost": currency, "peak_import_kw": "kW"},
        "omitted_objectives": {},
    }
    return {
        "runtime_revision": "r1",
        "baseline": baseline,
        "variations": {
            "v": {
                "scenario_id": "variation",
                "status": "no_action_required",
                "summary": summary,
                "diff": {"energy_cost": 1.0, "peak_import_kw": 0.0},
            }
        },
    }


@pytest.mark.parametrize(
    "case",
    [
        "currency_mismatch",
        "currency_unknown",
        "baseline_currency_unknown",
        "baseline_unit_missing",
        "nonapplicable_terminal",
        "baseline_metric_missing",
        "extension_unit_mismatch",
        "solar_convention_mismatch",
        "partial_physical_unit_missing",
        "legacy_monetary_baseline",
    ],
)
def test_a12_f01_reader_rejects_delta_without_compatible_baseline_evidence(case):
    from copy import deepcopy

    from pydantic import ValidationError

    from domoai.domain.product import ScenarioComparison

    payload = _f01_comparison_payload()
    base = payload["baseline"]
    outcome = payload["variations"]["v"]
    variation = outcome["summary"]
    ba = base["constraint_summary"]["accounting"]
    va = variation["constraint_summary"]["accounting"]
    if case == "currency_mismatch":
        va["currency"] = "USD"
        va["objective_units"]["energy_cost"] = "USD"
        va["comparison"]["diff_units"]["energy_cost"] = "USD"
    elif case == "currency_unknown":
        va["currency"] = None
    elif case == "baseline_currency_unknown":
        ba["currency"] = None
    elif case == "baseline_unit_missing":
        ba["objective_units"].pop("energy_cost")
    elif case == "partial_physical_unit_missing":
        ba["objective_units"].pop("peak_import_kw")
    elif case == "baseline_metric_missing":
        base["objective_values"].pop("energy_cost")
        ba["objective_units"].pop("energy_cost")
    elif case == "legacy_monetary_baseline":
        base["constraint_summary"].pop("accounting")
    else:
        metric = {
            "nonapplicable_terminal": "terminal_soc_value_eur",
            "extension_unit_mismatch": "extension",
            "solar_convention_mismatch": "solar_self_consumption_kwh",
        }[case]
        for summary, block in [(base, ba), (variation, va)]:
            summary["objective_values"][metric] = 0.0
            block["objective_units"][metric] = "EUR" if case == "nonapplicable_terminal" else "kWh"
        if case == "extension_unit_mismatch":
            ba["objective_units"][metric] = "kW"
        if case == "solar_convention_mismatch":
            base["constraint_summary"]["solar_accounting"] = {
                "convention": {"accounting_version": "solar-v1"}
            }
            variation["constraint_summary"]["solar_accounting"] = {
                "convention": {"accounting_version": "solar-v2"}
            }
        va["comparison"]["diff_units"][metric] = va["objective_units"][metric]
        outcome["diff"][metric] = 0.0
    before = deepcopy(payload)
    with pytest.raises(ValidationError):
        ScenarioComparison.model_validate(payload)
    assert payload == before


@pytest.mark.parametrize(
    "case",
    [
        "EUR",
        "USD",
        "legacy",
        "partial",
        "legacy_physical",
        "partial_physical",
        "applied_terminal",
        "matching_extension",
    ],
)
def test_a12_f01_reader_preserves_valid_partial_legacy_and_typed_comparisons(case):
    from copy import deepcopy

    from domoai.domain.product import ScenarioComparison

    payload = _f01_comparison_payload("USD" if case == "USD" else "EUR")
    base = payload["baseline"]
    outcome = payload["variations"]["v"]
    va = outcome["summary"]["constraint_summary"]["accounting"]
    ba = base["constraint_summary"]["accounting"]
    if case == "legacy":
        base["constraint_summary"].pop("accounting")
        outcome["summary"]["constraint_summary"].pop("accounting")
    elif case == "partial":
        va.pop("comparison")
        va["objective_units"] = {}
        va["currency"] = None
    elif case in {"legacy_physical", "partial_physical"}:
        outcome["diff"].pop("energy_cost")
        va["comparison"]["diff_units"].pop("energy_cost")
        if case == "legacy_physical":
            base["constraint_summary"].pop("accounting")
        else:
            ba["currency"] = None
            ba["objective_units"].pop("energy_cost")
            va["currency"] = None
    elif case in {"applied_terminal", "matching_extension"}:
        metric = "terminal_soc_value_eur" if case == "applied_terminal" else "extension"
        for summary, block in [(base, ba), (outcome["summary"], va)]:
            summary["objective_values"][metric] = 0.0
            block["objective_units"][metric] = "EUR"
            if case == "applied_terminal":
                block["terminal_value_applied"] = True
        va["comparison"]["diff_units"][metric] = "EUR"
        outcome["diff"][metric] = 0.0
    before = deepcopy(payload)
    result = ScenarioComparison.model_validate(payload)
    assert result.variations["v"].diff == outcome["diff"]
    assert ScenarioComparison.model_validate_json(result.model_dump_json()) == result
    assert payload == before
