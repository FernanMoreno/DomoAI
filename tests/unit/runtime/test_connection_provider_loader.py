from types import SimpleNamespace

from domoai.application.connection_providers import FixtureConnectionProvider
from domoai.runtime.connection_ports import ConnectionProviderManifest
from domoai.runtime.connection_provider_loader import ConnectionProviderLoader


def test_loader_reports_import_exception_without_exposing_exception_text() -> None:
    point = SimpleNamespace(
        group="domoai.connection_providers",
        name="raises",
        load=lambda: (_ for _ in ()).throw(RuntimeError("password=hidden")),
    )

    result = ConnectionProviderLoader.load([point])

    assert result.providers == ()
    assert result.diagnostics[0].code == "provider_entry_point_load_failed"
    assert "hidden" not in result.diagnostics[0].message


def test_loader_accepts_factory_returning_provider() -> None:
    provider = FixtureConnectionProvider(
        ConnectionProviderManifest(
            connector_id="factory-sdk",
            display_name="Factory SDK",
            protocol="fixture",
            version="1.0.0",
        ),
        adapter_factory=lambda: object(),
    )
    point = SimpleNamespace(
        group="domoai.connection_providers", name="factory", load=lambda: lambda: provider
    )

    result = ConnectionProviderLoader.load([point])

    assert result.providers[0] is provider
