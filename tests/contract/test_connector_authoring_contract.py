from __future__ import annotations

import inspect

from domoai.admin.cli import main
from domoai.admin.connector_scaffold import (
    ConnectorScaffoldRequest,
    check_connector_scaffold,
    create_connector_scaffold,
)
from domoai.runtime.connection_provider_base import ConnectionProviderBase


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
