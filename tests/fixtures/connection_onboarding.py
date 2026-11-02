"""Reusable fixtures for connector catalog and onboarding boundary tests."""

from __future__ import annotations

from domoai.application.connection_providers import (
    ConnectionProviderRegistry,
    FixtureConnectionProvider,
)
from domoai.domain.connections import SetupFieldDescriptor, SetupFieldType, SetupStep
from domoai.runtime.connection_ports import ConnectionProviderManifest


def simple_provider_manifest(
    connector_id: str = "fixture_onboarding",
) -> ConnectionProviderManifest:
    return ConnectionProviderManifest(
        connector_id=connector_id,
        display_name="Fixture Onboarding",
        protocol="fixture",
        version="1.0",
        supports_discovery=False,
        steps=(
            SetupStep(
                step_id="connection",
                title="Connection",
                input_keys=("url", "token"),
                secret_input_keys=("token",),
                fields=(
                    SetupFieldDescriptor(
                        key="url", label="URL", input_type=SetupFieldType.URL
                    ),
                    SetupFieldDescriptor(
                        key="token",
                        label="Token",
                        input_type=SetupFieldType.PASSWORD,
                        secret=True,
                    ),
                ),
            ),
        ),
    )


def simple_provider_registry(
    connector_id: str = "fixture_onboarding",
) -> ConnectionProviderRegistry:
    registry = ConnectionProviderRegistry()
    registry.register(
        FixtureConnectionProvider(
            simple_provider_manifest(connector_id), adapter_factory=lambda: object()
        )
    )
    return registry


__all__ = ["simple_provider_manifest", "simple_provider_registry"]
