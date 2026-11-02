from __future__ import annotations

import inspect
from pathlib import Path

from domoai.admin.cli import main
from domoai.admin.connector_scaffold import (
    ConnectorScaffoldRequest,
    check_connector_scaffold,
    create_connector_scaffold,
    load_connector_recipe,
)
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.runtime.connection_provider_base import ConnectionProviderBase

ROOT = Path(__file__).resolve().parents[2]


def test_base_provider_exposes_all_five_safe_extension_points() -> None:
    methods = ("discover", "probe", "validate", "build_adapter", "diagnose")

    assert all(
        inspect.iscoroutinefunction(getattr(ConnectionProviderBase, name)) for name in methods
    )


def test_scaffold_creates_installable_provider_project_with_incomplete_status(tmp_path) -> None:
    output = tmp_path / "demo-connector"
    result = create_connector_scaffold(
        ConnectorScaffoldRequest(
            connector_id="demo_connector",
            display_name="Demo Connector",
            protocol="demo",
            output=output,
        )
    )

    assert result.status == "created"
    assert (output / "pyproject.toml").is_file()
    assert (output / "src" / "demo_connector" / "provider.py").is_file()
    assert (output / "tests" / "test_provider_contract.py").is_file()
    assert "domoai.connection_providers" in (output / "pyproject.toml").read_text()

    check = check_connector_scaffold(output)
    assert check.status == "incomplete"
    assert "provider_adapter_not_implemented" in {item.code for item in check.diagnostics}


def test_admin_cli_exposes_create_and_check_commands(tmp_path, capsys) -> None:
    output = tmp_path / "cli-connector"

    assert main(
        [
            "connector",
            "create",
            "--connector-id",
            "cli_connector",
            "--display-name",
            "CLI Connector",
            "--protocol",
            "cli",
            "--output",
            str(output),
        ]
    ) == 0
    assert '"status": "created"' in capsys.readouterr().out

    assert main(["connector", "check", str(output)]) == 2
    assert '"status": "incomplete"' in capsys.readouterr().out


def test_recipe_scaffold_generates_recipe_metadata_and_delegating_provider(tmp_path) -> None:
    output = tmp_path / "recipe-connector"
    recipe = ConnectorRecipe.model_validate(
        {
            "connector_id": "recipe_connector",
            "display_name": "Recipe Connector",
            "transport_family": "mqtt",
            "discovery_strategies": ["manual"],
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "state_topic": "recipe/{device_id}/state",
                }
            ],
        }
    )

    result = create_connector_scaffold(
        ConnectorScaffoldRequest(
            connector_id="recipe_connector",
            display_name="Recipe Connector",
            protocol="mqtt",
            output=output,
            recipe=recipe,
        )
    )

    assert "recipe.yaml" in result.files
    assert (output / "recipe.yaml").is_file()
    assert "RecipeConnectionProvider" in (output / "src/recipe_connector/provider.py").read_text()
    assert check_connector_scaffold(output).status == "incomplete"


def test_recipe_scaffold_is_available_from_cli(tmp_path, capsys) -> None:
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(
        """schema_version: v1
connector_id: cli_recipe
display_name: CLI Recipe
transport_family: mqtt
capabilities:
  - capability_id: power
    kind: boolean
    state_topic: cli/{device_id}/state
""",
        encoding="utf-8",
    )
    output = tmp_path / "cli-recipe"

    assert main(["connector", "create", "--recipe", str(recipe), "--output", str(output)]) == 0
    assert '"connector_id": "cli_recipe"' in capsys.readouterr().out
    assert (output / "recipe.yaml").is_file()


def test_protocol_recipe_scaffold_wires_bounded_engines(tmp_path) -> None:
    expected_imports = {
        "modbus": ("ModbusRecipeTransport", "ModbusRecipeDiscovery"),
        "knx": ("KnxRecipeTransport", "KnxRecipeDiscovery"),
        "matter": ("MatterRecipeTransport", "MatterRecipeDiscovery"),
    }

    example_names = {
        "modbus": "modbus-temperature.yaml",
        "knx": "knx-light.yaml",
        "matter": "matter-light.yaml",
    }
    for family, imports in expected_imports.items():
        recipe = load_connector_recipe(ROOT / "examples" / "connectors" / example_names[family])
        output = tmp_path / family
        create_connector_scaffold(
            ConnectorScaffoldRequest(
                connector_id=recipe.connector_id,
                display_name=recipe.display_name,
                protocol=family,
                output=output,
                recipe=recipe,
            )
        )

        provider_source = (output / "src" / recipe.connector_id / "provider.py").read_text()
        assert all(symbol in provider_source for symbol in imports)
        assert check_connector_scaffold(output).status == "incomplete"
