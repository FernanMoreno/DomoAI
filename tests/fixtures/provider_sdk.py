"""Deterministic fixtures for the universal Provider SDK contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from domoai.domain.models import Capability, CapabilityKind, DeviceType, ExecutionStatus, SourceRef
from domoai.domain.provider import (
    DeviceDescriptor,
    Measurement,
    ProviderCommand,
    ProviderExecutionResult,
    ProviderManifest,
    ProviderRole,
)
from domoai.runtime.execution_context import ExecutionContext

OBSERVED_AT = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 8, 16, 12, 0, 1, tzinfo=UTC)


def telemetry_manifest(provider_id: str = "fixture_telemetry") -> ProviderManifest:
    return ProviderManifest(
        provider_id=provider_id,
        name="Fixture telemetry provider",
        protocol="fixture",
        package_name="domoai-fixture",
        package_version="1.0.0",
        roles=[ProviderRole.TELEMETRY],
        device_types=[DeviceType.ENERGY],
        capabilities=[
            Capability(
                name="energy.pv.power",
                kind=CapabilityKind.NUMBER,
                unit="W",
                readable=True,
                writable=False,
            ),
            Capability(
                name="energy.grid.power",
                kind=CapabilityKind.NUMBER,
                unit="W",
                readable=True,
                writable=False,
            ),
            Capability(
                name="battery.soc",
                kind=CapabilityKind.NUMBER,
                unit="%",
                readable=True,
                writable=False,
                minimum=0,
                maximum=100,
            ),
        ],
    )


def command_manifest(provider_id: str = "fixture_commands") -> ProviderManifest:
    return ProviderManifest(
        provider_id=provider_id,
        name="Fixture command provider",
        protocol="fixture",
        package_name="domoai-fixture",
        package_version="1.0.0",
        roles=[ProviderRole.COMMANDS],
        device_types=[DeviceType.LIGHT],
        capabilities=[
            Capability(
                name="power",
                kind=CapabilityKind.BOOLEAN,
                readable=True,
                writable=True,
                commands=["turn_on", "turn_off"],
            )
        ],
    )


class TelemetryFixture:
    def __init__(self, manifest: ProviderManifest | None = None) -> None:
        self.manifest = manifest or telemetry_manifest()

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def discover(self) -> list[DeviceDescriptor]:
        return [
            DeviceDescriptor(
                provider_id=self.manifest.provider_id,
                external_id="inverter.main",
                device_type=DeviceType.ENERGY,
                name="Virtual inverter",
                manufacturer="Fixture",
                model="PV-1",
                serial_number="fixture-001",
                area_id="garage",
                capabilities=self.manifest.capabilities,
                identity_keys=["fixture:inverter.main"],
                connections=["fixture:bus:1"],
            )
        ]

    async def get_measurements(self, device_ids: Sequence[str] | None = None) -> list[Measurement]:
        if device_ids is not None and "inverter.main" not in device_ids:
            return []
        source = SourceRef(adapter_id=self.manifest.provider_id, external_id="inverter.main")
        return [
            Measurement(
                provider_id=self.manifest.provider_id,
                device_id="inverter.main",
                metric="energy.pv.power",
                value=4382,
                unit="W",
                observed_at=OBSERVED_AT,
                received_at=RECEIVED_AT,
                source_ref=source,
            ),
            Measurement(
                provider_id=self.manifest.provider_id,
                device_id="inverter.main",
                metric="energy.grid.power",
                value=-728,
                unit="W",
                observed_at=OBSERVED_AT,
                received_at=RECEIVED_AT,
                source_ref=source,
            ),
            Measurement(
                provider_id=self.manifest.provider_id,
                device_id="inverter.main",
                metric="battery.soc",
                value=74,
                unit="%",
                observed_at=OBSERVED_AT,
                received_at=RECEIVED_AT,
                source_ref=source,
            ),
        ]

    async def subscribe(self) -> AsyncIterator[Measurement]:
        if False:
            yield Measurement(
                provider_id=self.manifest.provider_id,
                device_id="inverter.main",
                metric="energy.pv.power",
                value=0,
                unit="W",
                observed_at=OBSERVED_AT,
                received_at=RECEIVED_AT,
                source_ref=SourceRef(
                    adapter_id=self.manifest.provider_id,
                    external_id="inverter.main",
                ),
            )


class CommandFixture:
    def __init__(self, manifest: ProviderManifest | None = None) -> None:
        self.manifest = manifest or command_manifest()
        self.commands: list[ProviderCommand] = []
        self.execution_contexts: list[ExecutionContext | None] = []

    async def execute(
        self,
        command: ProviderCommand,
        execution_context: ExecutionContext | None = None,
    ) -> ProviderExecutionResult:
        self.commands.append(command)
        self.execution_contexts.append(execution_context)
        return ProviderExecutionResult(
            provider_id=self.manifest.provider_id,
            external_device_id=command.external_device_id,
            command=command.command,
            status=ExecutionStatus.CONFIRMED_SUCCESS,
            completed_at=RECEIVED_AT,
            source_ref=SourceRef(
                adapter_id=self.manifest.provider_id,
                external_id=command.external_device_id,
            ),
        )


class FailingTelemetryFixture(TelemetryFixture):
    async def get_measurements(self, device_ids: Sequence[str] | None = None) -> list[Measurement]:
        raise ConnectionError("fixture transport contains token=must-not-leak")


class FailingDiscoveryTelemetryFixture(TelemetryFixture):
    async def discover(self) -> list[DeviceDescriptor]:
        raise TimeoutError("fixture discovery contains password=must-not-leak")
