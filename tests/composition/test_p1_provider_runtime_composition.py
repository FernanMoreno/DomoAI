from types import SimpleNamespace

from domoai.application.connection_providers import (
    ConnectionProviderRegistry,
    FixtureConnectionProvider,
)
from domoai.runtime.connection_ports import ConnectionProviderManifest
from domoai.runtime.connection_provider_loader import ConnectionProviderLoader


def test_provider_loader_and_registry_preserve_zero_provider_boot() -> None:
    empty = ConnectionProviderRegistry.from_entry_points([])
    point = SimpleNamespace(
        group="domoai.connection_providers",
        name="invalid",
        load=lambda: object(),
    )
    result = ConnectionProviderLoader.load([point])
    valid = FixtureConnectionProvider(
        ConnectionProviderManifest(
            connector_id="fixture-composition",
            display_name="Fixture",
            protocol="fixture",
            version="1.0.0",
        ),
        adapter_factory=lambda: object(),
    )
    registry = ConnectionProviderRegistry.from_entry_points(
        [SimpleNamespace(group="domoai.connection_providers", name="valid", load=lambda: valid)]
    )

    assert empty.providers == ()
    assert result.providers == ()
    assert registry.get("fixture-composition") is valid
