import pytest
from tests.fixtures.provider_sdk import (
    CommandFixture,
    FailingDiscoveryTelemetryFixture,
    FailingTelemetryFixture,
    TelemetryFixture,
    command_manifest,
    telemetry_manifest,
)

from domoai.domain.provider import ProviderCommand
from domoai.runtime.provider_sdk import (
    DuplicateProviderError,
    ProviderRegistry,
    ProviderRegistryError,
)


@pytest.mark.asyncio
async def test_registry_orders_providers_and_preserves_healthy_collection() -> None:
    registry = ProviderRegistry()
    failing = FailingTelemetryFixture(
        manifest=telemetry_manifest("fixture_failing")
    )
    healthy = TelemetryFixture()
    commands = CommandFixture()
    registry.register(failing)
    registry.register(healthy)
    registry.register(commands)

    assert [provider.manifest.provider_id for provider in registry.providers] == [
        "fixture_commands",
        "fixture_failing",
        "fixture_telemetry",
    ]

    collected = await registry.collect()

    assert [measurement.metric for measurement in collected.measurements] == [
        "energy.pv.power",
        "energy.grid.power",
        "battery.soc",
    ]
    assert len(collected.diagnostics) == 1
    assert collected.diagnostics[0].code == "collection_failed"
    assert "token" not in collected.diagnostics[0].message
    assert collected.diagnostics[0].retryable is True


@pytest.mark.asyncio
async def test_registry_discovery_keeps_healthy_descriptors_when_one_fails() -> None:
    registry = ProviderRegistry()
    registry.register(FailingDiscoveryTelemetryFixture())
    registry.register(TelemetryFixture(manifest=telemetry_manifest("fixture_z")))

    discovered = await registry.discover_all()

    assert [device.external_id for device in discovered.devices] == ["inverter.main"]
    assert [diagnostic.provider_id for diagnostic in discovered.diagnostics] == [
        "fixture_telemetry"
    ]


def test_registry_rejects_duplicate_provider_ids() -> None:
    registry = ProviderRegistry()
    registry.register(TelemetryFixture())

    with pytest.raises(DuplicateProviderError):
        registry.register(TelemetryFixture())


def test_registry_rejects_provider_with_missing_role_methods() -> None:
    class IncompleteProvider:
        manifest = command_manifest("fixture_incomplete")

    registry = ProviderRegistry()
    with pytest.raises(TypeError, match="missing execute"):
        registry.register(IncompleteProvider())


@pytest.mark.asyncio
async def test_registry_routes_commands_and_rejects_telemetry_only_without_invocation() -> None:
    registry = ProviderRegistry()
    telemetry = TelemetryFixture()
    command_provider = CommandFixture()
    registry.register(telemetry)
    registry.register(command_provider)
    command = ProviderCommand(
        provider_id="fixture_commands",
        external_device_id="living_room.main_light",
        command="turn_on",
        idempotency_key="command-1",
    )

    result = await registry.execute("fixture_commands", command)

    assert result.status.value == "confirmed_success"
    assert len(command_provider.commands) == 1
    with pytest.raises(ProviderRegistryError, match="does not support commands"):
        await registry.execute(
            "fixture_telemetry", command.model_copy(update={"provider_id": "fixture_telemetry"})
        )
    assert len(command_provider.commands) == 1


@pytest.mark.asyncio
async def test_registry_converts_provider_command_failure_to_safe_result() -> None:
    class FailingCommandProvider(CommandFixture):
        async def execute(self, command: ProviderCommand):
            raise RuntimeError("response body contains password=must-not-leak")

    registry = ProviderRegistry()
    registry.register(FailingCommandProvider())
    command = ProviderCommand(
        provider_id="fixture_commands",
        external_device_id="living_room.main_light",
        command="turn_on",
        idempotency_key="command-2",
    )

    result = await registry.execute("fixture_commands", command)

    assert result.status.value == "failed"
    assert result.message == "provider command failed"
    assert "password" not in result.message
