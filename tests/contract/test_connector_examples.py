from __future__ import annotations

from pathlib import Path

from domoai.admin.connector_scaffold import load_connector_recipe

ROOT = Path(__file__).resolve().parents[2]


def test_protocol_examples_are_strict_and_secret_free() -> None:
    paths = sorted((ROOT / "examples" / "connectors").glob("*.yaml"))
    assert {path.name for path in paths} >= {
        "modbus-temperature.yaml",
        "knx-light.yaml",
        "matter-light.yaml",
    }
    for path in paths:
        recipe = load_connector_recipe(path)
        payload = recipe.model_dump_json()
        assert "client_secret" not in payload
        assert "access_token" not in payload
        assert recipe.protocol_profile is not None
