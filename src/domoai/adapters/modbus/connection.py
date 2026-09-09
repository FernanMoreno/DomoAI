"""Connection provider for bounded Modbus TCP identification and profiles."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import cast

from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.catalog import ModbusProfileCatalog
from domoai.adapters.modbus.identification import ModbusIdentificationService
from domoai.adapters.modbus.transport import ModbusTransport
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDiagnostic,
    DiagnosticSeverity,
    InventoryStatus,
    ProbeResult,
    ProductStatus,
    SetupFieldDescriptor,
    SetupFieldType,
    SetupStep,
    TransportStatus,
    ValidationResult,
)
from domoai.domain.p1_completion import ModbusDeviceIdentity
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.ports import AdapterPort

TransportFactory = Callable[[str, int], ModbusTransport | Awaitable[ModbusTransport]]


class ModbusConnectionProvider:
    manifest = ConnectionProviderManifest(
        connector_id="modbus",
        display_name="Modbus TCP",
        protocol="modbus-tcp",
        version="v1",
        steps=(
            SetupStep(
                step_id="endpoint",
                title="Modbus TCP endpoint",
                input_keys=("host", "port"),
                fields=(
                    SetupFieldDescriptor(
                        key="host", label="Device host", input_type=SetupFieldType.TEXT
                    ),
                    SetupFieldDescriptor(
                        key="port", label="Device port", input_type=SetupFieldType.NUMBER
                    ),
                ),
            ),
        ),
    )

    def __init__(
        self,
        transport_factory: TransportFactory,
        *,
        default_host: str = "127.0.0.1",
        default_port: int = 502,
        unit_ids: Iterable[int] = (1,),
        timeout: float = 5.0,
        profile_catalog: ModbusProfileCatalog | None = None,
    ) -> None:
        if not default_host or len(default_host) > 128 or ":" in default_host:
            raise ValueError("Modbus host must be a bounded hostname or address reference")
        if not 1 <= default_port <= 65535 or timeout <= 0:
            raise ValueError("Modbus endpoint or timeout is invalid")
        self._transport_factory = transport_factory
        self.default_host = default_host
        self.default_port = default_port
        self.unit_ids = tuple(unit_ids)
        self.identification_timeout = timeout
        self.profile_catalog = profile_catalog or ModbusProfileCatalog()
        # Validate bounded policy once at provider construction.
        ModbusIdentificationService(
            cast(ModbusTransport, _UnconnectedTransport()),
            unit_ids=self.unit_ids,
            timeout=timeout,
        )

    async def discover(
        self, host: str | None = None, port: int | None = None
    ) -> list[ConnectionCandidate]:
        selected_host = host or self.default_host
        selected_port = port or self.default_port
        if not selected_host or len(selected_host) > 128 or not 1 <= selected_port <= 65535:
            raise ValueError("Modbus endpoint is invalid")
        return [
            ConnectionCandidate(
                connector_id="modbus",
                candidate_id=f"modbus:{selected_host}:{selected_port}",
                display_name=self.manifest.display_name,
                endpoint={"host": selected_host, "port": selected_port},
                metadata={
                    "discovery": "configured_tcp",
                    "unit_ids": ",".join(map(str, self.unit_ids)),
                },
            )
        ]

    async def identify(self, candidate: ConnectionCandidate) -> ModbusDeviceIdentity:
        transport = await self._transport_for(candidate)
        try:
            if not await transport.health():
                await transport.connect()
            return await ModbusIdentificationService(
                transport,
                unit_ids=self.unit_ids,
                timeout=self.identification_timeout,
            ).identify(f"modbus:{candidate.candidate_id}")
        finally:
            await transport.disconnect()

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        if candidate.connector_id != "modbus":
            return self._failure("modbus_provider_mismatch", "candidate does not belong to Modbus")
        try:
            identity = await self.identify(candidate)
            if not identity.complete:
                return ProbeResult(
                    transport_status=TransportStatus.REACHABLE,
                    inventory_status=InventoryStatus.MAPPING_REQUIRED,
                    product_status=ProductStatus.NEEDS_SETUP,
                    diagnostics=(
                        self._diagnostic(
                            "modbus_profile_required",
                            "complete Modbus Device Identification is required",
                        ),
                    ),
                )
            resolution = self.profile_catalog.resolve(identity)
            if resolution.status.value != "matched":
                return ProbeResult(
                    transport_status=TransportStatus.REACHABLE,
                    inventory_status=InventoryStatus.MAPPING_REQUIRED,
                    product_status=ProductStatus.NEEDS_SETUP,
                    diagnostics=(
                        self._diagnostic(
                            "modbus_profile_required", resolution.diagnostic or "profile required"
                        ),
                    ),
                )
            return ProbeResult(
                transport_status=TransportStatus.CONNECTED,
                inventory_status=InventoryStatus.READY,
                product_status=ProductStatus.CONNECTED,
            )
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            return self._failure("modbus_probe_failed", str(error))

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        if setup.candidate.connector_id != "modbus" or setup.draft.connector_id != "modbus":
            return ValidationResult(
                valid=False,
                diagnostics=(
                    self._diagnostic("modbus_provider_mismatch", "setup does not belong to Modbus"),
                ),
            )
        try:
            identity = await self.identify(setup.candidate)
            resolution = self.profile_catalog.resolve(identity)
            if resolution.status.value != "matched":
                return ValidationResult(
                    valid=False,
                    diagnostics=(
                        self._diagnostic(
                            "modbus_profile_required", resolution.diagnostic or "profile required"
                        ),
                    ),
                )
            profile = self.profile_catalog.get(resolution.profile_id or "")
            if profile is None:
                raise ValueError("resolved Modbus profile is unavailable")
            normalized = setup.draft.model_copy(
                update={
                    "options": {
                        **setup.draft.options,
                        "unit_id": identity.unit_id,
                        "vendor_name": identity.vendor_name,
                        "product_code": identity.product_code,
                        "revision": identity.revision,
                        "profile_id": profile.profile_id,
                        "profile_signature": profile.signature,
                    }
                }
            )
            return ValidationResult(valid=True, normalized_draft=normalized)
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            return ValidationResult(
                valid=False,
                diagnostics=(self._diagnostic("modbus_validation_failed", str(error)),),
            )

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort:
        validation = await self.validate(setup)
        if not validation.valid or validation.normalized_draft is None:
            raise ValueError("Modbus connection setup is invalid")
        profile_id = str(validation.normalized_draft.options.get("profile_id", ""))
        profile = self.profile_catalog.get(profile_id)
        if profile is None:
            raise ValueError("Modbus profile is required before adapter build")
        transport = await self._transport_for(setup.candidate)
        return ModbusAdapter(transport, profile.mapping, adapter_id=adapter_id)

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]:
        return (await self.validate(setup)).diagnostics

    async def _transport_for(self, candidate: ConnectionCandidate) -> ModbusTransport:
        host = candidate.endpoint.get("host")
        port = candidate.endpoint.get("port")
        if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool):
            raise ValueError("Modbus candidate endpoint is invalid")
        result = self._transport_factory(host, port)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _diagnostic(code: str, reason: str) -> ConnectionDiagnostic:
        return ConnectionDiagnostic(
            code=code,
            severity=DiagnosticSeverity.ERROR,
            retryable=code.endswith("failed"),
            message="Modbus connection validation failed",
            technical_details={"reason": reason[:180]},
        )

    @classmethod
    def _failure(cls, code: str, reason: str) -> ProbeResult:
        return ProbeResult(
            transport_status=TransportStatus.UNREACHABLE,
            inventory_status=InventoryStatus.FAILED,
            product_status=ProductStatus.UNAVAILABLE,
            diagnostics=(cls._diagnostic(code, reason),),
        )


class _UnconnectedTransport:
    async def identify_device(self, unit_id: int) -> None:
        return None


__all__ = ["ModbusConnectionProvider"]
