from __future__ import annotations

import pytest

from domoai.domain.models import ExecutionStatus
from domoai.domain.provider import ProviderCommand
from domoai.runtime.provider_sdk import ProviderRegistry
from tests.fixtures.provider_sdk import (
    CommandFixture,
    FailingTelemetryFixture,
    TelemetryFixture,
    command_manifest,
    telemetry_manifest,
)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_provider_registry_preserves_provenance_and_sanitizes_failure_boundary() -> None:
    telemetry = TelemetryFixture(telemetry_manifest("telemetry-composition"))
    commands = CommandFixture(command_manifest("commands-composition"))
    failing = FailingTelemetryFixture(telemetry_manifest("failing-composition"))
    registry = ProviderRegistry()
    registry.register(telemetry)
    registry.register(commands)
    registry.register(failing)

    collection = await registry.collect(["inverter.main"])
    assert collection.measurements
    assert all(item.provider_id == "telemetry-composition" for item in collection.measurements)
    assert collection.diagnostics[0].provider_id == "failing-composition"
    assert "token" not in collection.diagnostics[0].message

    result = await registry.execute(
        "commands-composition",
        ProviderCommand(
            provider_id="commands-composition",
            external_device_id="light-1",
            command="turn_on",
            idempotency_key="provider-contract-command",
        ),
    )
    assert result.status is ExecutionStatus.CONFIRMED_SUCCESS
    assert result.provider_id == "commands-composition"
