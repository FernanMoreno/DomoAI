from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from domoai.adapters.zigbee2mqtt.connection import Zigbee2MqttConnectionProvider
from domoai.application.connection_providers import (
    ConnectionProviderManifest,
    ConnectionProviderRegistry,
    ConnectionSetup,
    FixtureConnectionProvider,
    OnboardingCoordinator,
)
from domoai.domain.connections import (
    ConnectionDraft,
    DiagnosticSeverity,
    InventoryStatus,
    ProductStatus,
    TransportStatus,
    ValidationResult,
)
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
)


class NoopAdapter:
    adapter_id = "connection:fixture"

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def discover(self) -> AdapterSnapshot:
        return AdapterSnapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        return []

    async def execute(
        self, command: Command, execution_context: object | None = None
    ) -> AdapterExecutionAck:
        return AdapterExecutionAck(accepted=False)

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        async def events() -> AsyncIterator[SourceEvent]:
            if False:
                yield SourceEvent  # type: ignore[misc]

        return events()

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=True)


@pytest.mark.asyncio
async def test_fixture_provider_completes_discover_probe_validate_build() -> None:
    manifest = ConnectionProviderManifest(
        connector_id="fixture",
        display_name="Fixture",
        protocol="fixture",
        version="1.0",
    )
    provider = FixtureConnectionProvider(manifest, adapter_factory=NoopAdapter)
    registry = ConnectionProviderRegistry()
    registry.register(provider)

    candidates = await provider.discover()
    assert candidates[0].connector_id == "fixture"
    probe = await provider.probe(candidates[0])
    assert probe.transport_status is TransportStatus.REACHABLE
    assert probe.inventory_status is InventoryStatus.READY
    assert probe.product_status is ProductStatus.CONNECTED

    draft = ConnectionDraft(
        connector_id="fixture",
        unique_id=candidates[0].candidate_id,
        endpoint={"host": "fixture"},
        options={},
    )
    setup = ConnectionSetup(candidate=candidates[0], draft=draft)
    validation = await provider.validate(setup)
    assert isinstance(validation, ValidationResult)
    assert validation.valid
    adapter = await provider.build_adapter(setup, adapter_id="connection:fixture")
    assert adapter.adapter_id == "connection:fixture"
    assert registry.get("fixture") is provider


@pytest.mark.asyncio
async def test_onboarding_coordinator_enforces_discover_select_setup_validate_build() -> None:
    manifest = ConnectionProviderManifest(
        connector_id="fixture",
        display_name="Fixture",
        protocol="fixture",
        version="1.0",
    )
    registry = ConnectionProviderRegistry()
    registry.register(FixtureConnectionProvider(manifest, adapter_factory=NoopAdapter))
    flow = OnboardingCoordinator(registry)

    session_id, candidates = await flow.start("fixture")
    await flow.select(session_id, candidates[0].candidate_id)
    draft = ConnectionDraft(
        connector_id="fixture",
        unique_id=candidates[0].candidate_id,
        endpoint={"host": "fixture"},
        options={},
    )
    await flow.setup(session_id, draft)
    assert (await flow.validate(session_id)).valid
    adapter = await flow.build(session_id, adapter_id="connection:flow")
    assert adapter.adapter_id == "connection:flow"


def test_provider_registry_rejects_duplicate_connector_ids() -> None:
    manifest = ConnectionProviderManifest(
        connector_id="fixture",
        display_name="Fixture",
        protocol="fixture",
        version="1.0",
    )
    registry = ConnectionProviderRegistry()
    registry.register(FixtureConnectionProvider(manifest, adapter_factory=NoopAdapter))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FixtureConnectionProvider(manifest, adapter_factory=NoopAdapter))


def test_provider_manifest_and_diagnostics_are_strict_and_secret_safe() -> None:
    with pytest.raises(ValueError):
        ConnectionProviderManifest(
            connector_id="Fixture",
            display_name="Fixture",
            protocol="fixture",
            version="1.0",
        )
    diagnostic = {
        "code": "auth_failed",
        "severity": DiagnosticSeverity.ERROR,
        "retryable": False,
        "message": "token:secret-value",
    }
    with pytest.raises(ValueError):
        from domoai.domain.connections import ConnectionDiagnostic

        ConnectionDiagnostic.model_validate(diagnostic)


def test_zigbee2mqtt_provider_exposes_the_v1_onboarding_manifest() -> None:
    provider = Zigbee2MqttConnectionProvider(secret_resolver=lambda reference: reference)

    assert provider.manifest.connector_id == "zigbee2mqtt"
    assert provider.manifest.protocol == "mqtt"
    assert {step.step_id for step in provider.manifest.steps} == {
        "broker",
        "credentials",
        "base_topic",
    }
