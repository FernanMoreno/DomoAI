import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
