from __future__ import annotations

from types import SimpleNamespace

from domoai.application.connection_providers import (
    ConnectionProviderRegistry,
    FixtureConnectionProvider,
)
from domoai.runtime.connection_ports import ConnectionProviderManifest
from domoai.runtime.connection_provider_loader import (
    CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
    ConnectionProviderLoader,
)


def _fixture(connector_id: str) -> FixtureConnectionProvider:
    return FixtureConnectionProvider(
        ConnectionProviderManifest(
            connector_id=connector_id,
            display_name=connector_id,
            protocol="fixture",
            version="1.0",
        ),
        adapter_factory=lambda: object(),
    )


def test_catalog_and_loader_keep_provider_families_isolated() -> None:
    registry = ConnectionProviderRegistry()
    registry.register(_fixture("family_a"))
    registry.register(_fixture("family_b"))

    broken = SimpleNamespace(
        group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
        name="broken",
        load=lambda: object(),
    )
    good = SimpleNamespace(
        group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
        name="family_c",
        load=lambda: _fixture("family_c"),
    )
    loaded = ConnectionProviderLoader.load([broken, good])

    assert [entry.connector_id for entry in registry.catalog] == ["family_a", "family_b"]
    assert [provider.manifest.connector_id for provider in loaded.providers] == ["family_c"]
    assert loaded.diagnostics[0].code == "provider_entry_point_invalid"

