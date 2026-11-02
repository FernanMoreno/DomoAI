import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_coverage_evidence_schema_is_separate_strict_and_non_qualifying() -> None:
    from domoai.domain.coverage import CoverageEvidenceSnapshot

    path = ROOT / "schemas" / "v1" / "coverage-evidence.schema.json"
    assert path.exists()
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema == CoverageEvidenceSnapshot.model_json_schema(by_alias=True)
    assert schema["properties"]["schema_version"]["const"] == "v1"
    assert schema["additionalProperties"] is False
    evidence = schema["$defs"]["UnknownCoverageEvidence"]
    assert evidence["properties"]["status"]["const"] == "not_verified"
    assert evidence["properties"]["firmware"]["type"] == "null"
    assert evidence["properties"]["verified_capabilities"]["maxItems"] == 0


def test_energy_contract_schemas_are_published_and_versioned() -> None:
    energy_context = json.loads(
        (ROOT / "schemas" / "v1" / "energy-context.schema.json").read_text(encoding="utf-8")
    )
    optimization = json.loads(
        (ROOT / "schemas" / "v1" / "optimization-scenario.schema.json").read_text(encoding="utf-8")
    )

    assert energy_context["properties"]["schema_version"]["const"] == "v1"
    assert "energy_context" in optimization["properties"]
    assert (ROOT / "schemas" / "v1" / "battery-profile.schema.json").exists()


def test_adapter_sdk_manifest_schema_is_published_and_versioned() -> None:
    manifest = json.loads(
        (ROOT / "schemas" / "v1" / "adapter-manifest.schema.json").read_text(encoding="utf-8")
    )

    assert manifest["properties"]["schema_version"]["const"] == "v1"
    assert "capabilities" in manifest["properties"]


def test_battery_simulator_schemas_are_published_and_lab_only() -> None:
    profile = json.loads(
        (ROOT / "schemas" / "v1" / "battery-simulation-profile.schema.json").read_text(
            encoding="utf-8"
        )
    )
    state = json.loads(
        (ROOT / "schemas" / "v1" / "battery-simulation-state.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert "capacity_kwh" in profile["properties"]
    assert "max_charge_kw" in profile["properties"]
    assert "soc_kwh" in state["properties"]
    assert "revision" in state["properties"]
    # Lab-only: no field ties this shape to a production qualification or
    # dispatch binding artifact.
    assert "profile_digest" not in state["properties"]
    assert "capacity_evidence" not in profile["properties"]


def test_connector_recipe_schema_is_published_and_secret_free() -> None:
    recipe = json.loads(
        (ROOT / "schemas" / "v1" / "connector-recipe.schema.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = json.loads(
        (ROOT / "schemas" / "v1" / "connection-provider-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert recipe["properties"]["schema_version"]["const"] == "v1"
    assert "transport_family" in recipe["properties"]
    assert "protocol_profile" in recipe["properties"]
    profile_schema = recipe["properties"]["protocol_profile"]
    profile_any = profile_schema.get("anyOf", profile_schema.get("oneOf", []))
    if profile_any and "oneOf" in profile_any[0]:
        profile_any = profile_any[0]["oneOf"]
    assert {item["$ref"].rsplit("/", 1)[-1] for item in profile_any if "$ref" in item} >= {
        "ModbusRecipeProfile",
        "KnxRecipeProfile",
        "MatterRecipeProfile",
    }
    assert "recipe" in manifest["properties"]
    assert "credential_value" not in recipe
    assert "client_secret" not in json.dumps(recipe)


def test_solar_accounting_schema_validates_actual_result_and_rejects_wrong_unit() -> None:
    import copy

    import jsonschema
    import pytest

    from domoai.optimizer.cp_sat import CpSatOptimizer
    from domoai.optimizer.solar_accounting import SolarAccountingEvidence
    from domoai.runtime.registry import DeviceRegistry
    from tests.integration.test_energy_optimization import solar_accounting_scenario

    path = ROOT / "schemas" / "v1" / "solar-accounting-evidence.schema.json"
    assert path.exists()
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema == SolarAccountingEvidence.model_json_schema(by_alias=True)
    result = CpSatOptimizer(DeviceRegistry()).optimize(solar_accounting_scenario())
    payload = result.constraint_summary["solar_accounting"]
    jsonschema.validate(payload, schema)
    forged = copy.deepcopy(payload)
    forged["convention"]["unit"] = "kW"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(forged, schema)


def test_runtime_schema_exports_preserve_all_existing_artifacts(tmp_path, monkeypatch) -> None:
    from scripts import export_schemas

    from tests.fixtures.runtime_schema_compatibility import FIXTURES

    before = {
        str(path.relative_to(ROOT / "schemas")): path.read_bytes()
        for path in (ROOT / "schemas").rglob("*.schema.json")
    }
    # Preserve hand-authored artifacts too; the exporter does not own them.
    for relative, raw in before.items():
        destination = tmp_path / "schemas" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    monkeypatch.setattr(export_schemas, "ROOT", tmp_path)
    monkeypatch.setattr(export_schemas, "OUTPUT", tmp_path / "schemas" / "v1")
    export_schemas.main()
    for relative, raw in before.items():
        assert (tmp_path / "schemas" / relative).read_bytes() == raw, relative
    families = {
        "plan",
        "automation-rule",
        "optimization-result",
        "automation-event",
        "physical-intent",
    }
    for family in families:
        assert (tmp_path / "schemas/v1" / f"{family}.schema.json").read_bytes() == (
            FIXTURES / f"{family}.schema.json"
        ).read_bytes()
        assert (tmp_path / "schemas/v2" / f"{family}.schema.json").exists()


def test_a12_accounting_has_one_standalone_version_one_schema():
    from scripts.export_schemas import MODELS

    from domoai.domain.product import AccountingEvidence

    schema_path = ROOT / "schemas/v1/optimization-accounting.schema.json"
    assert schema_path.exists()
    assert MODELS["optimization-accounting"] is AccountingEvidence
    schema = json.loads(schema_path.read_text())
    assert schema == AccountingEvidence.model_json_schema(by_alias=True)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["version"]["const"] == 1
    assert schema["properties"]["objective_units"]["maxProperties"] == 32
    assert "ComparisonAccounting" in schema["$defs"]
