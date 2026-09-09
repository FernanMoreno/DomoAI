from types import SimpleNamespace

import pytest

from domoai.application.connection_providers import (
    ConnectionProviderRegistry,
    FixtureConnectionProvider,
)
from domoai.runtime.connection_ports import ConnectionProviderManifest
from domoai.runtime.connection_provider_loader import (
    CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
    ConnectionProviderLoader,
)


def _provider(connector_id: str = "fixture-sdk") -> FixtureConnectionProvider:
    return FixtureConnectionProvider(
        ConnectionProviderManifest(
            connector_id=connector_id,
            display_name="SDK Fixture",
            protocol="fixture",
            version="1.0.0",
        ),
        adapter_factory=lambda: object(),
    )


def test_connection_provider_entry_point_group_and_conformance() -> None:
    good = SimpleNamespace(
        group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
        name="fixture",
        load=lambda: _provider(),
    )

    result = ConnectionProviderLoader.load([good])

    assert [provider.manifest.connector_id for provider in result.providers] == ["fixture-sdk"]
    assert result.diagnostics == ()


def test_invalid_provider_isolated_and_duplicate_diagnosed() -> None:
    broken = SimpleNamespace(
        group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
        name="broken",
        load=lambda: object(),
    )
    duplicate = SimpleNamespace(
        group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
        name="duplicate",
        load=lambda: _provider(),
    )
    second_duplicate = SimpleNamespace(
        group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
        name="duplicate-2",
        load=lambda: _provider(),
    )

    result = ConnectionProviderLoader.load([broken, duplicate, second_duplicate])

    assert [provider.manifest.connector_id for provider in result.providers] == ["fixture-sdk"]
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "provider_entry_point_invalid",
        "provider_duplicate",
    }


@pytest.mark.parametrize("group", ["domoai.adapters", "other"])
def test_loader_ignores_other_entry_point_groups(group: str) -> None:
    point = SimpleNamespace(group=group, name="ignored", load=lambda: object())

    result = ConnectionProviderLoader.load([point])

    assert result.providers == ()
    assert result.diagnostics == ()


def test_registry_can_combine_builtins_and_entry_points() -> None:
    point = SimpleNamespace(
        group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP,
        name="fixture",
        load=lambda: _provider("external-sdk"),
    )

    registry = ConnectionProviderRegistry.from_entry_points(
        [point], builtins=(_provider("builtin-sdk"),)
    )

    assert [provider.manifest.connector_id for provider in registry.providers] == [
        "builtin-sdk",
        "external-sdk",
    ]
