from __future__ import annotations

import sys
from types import SimpleNamespace

from domoai.admin.connector_scaffold import (
    ConnectorScaffoldRequest,
    check_connector_scaffold,
    create_connector_scaffold,
)
from domoai.runtime.connection_provider_loader import (
    CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
    ConnectionProviderLoader,
)


def test_generated_provider_loads_alongside_broken_entry_point(tmp_path) -> None:
    root = tmp_path / "generated"
    create_connector_scaffold(
        ConnectorScaffoldRequest("integration_connector", "Integration", "fixture", root)
    )
    sys.path.insert(0, str(root / "src"))
    try:
        from integration_connector.provider import create_provider

        good = SimpleNamespace(
            group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
            name="integration",
            load=lambda: create_provider,
        )
        broken = SimpleNamespace(
            group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
            name="broken",
            load=lambda: object(),
        )
        result = ConnectionProviderLoader.load([broken, good])
    finally:
        sys.path.remove(str(root / "src"))
        sys.modules.pop("integration_connector", None)
        sys.modules.pop("integration_connector.provider", None)

    assert [provider.manifest.connector_id for provider in result.providers] == [
        "integration_connector"
    ]
    assert result.diagnostics[0].code == "provider_entry_point_invalid"
    assert check_connector_scaffold(root).status == "incomplete"

