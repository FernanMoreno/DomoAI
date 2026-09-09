"""Connection provider for a local Matter Server controller endpoint."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Any

from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.transport import MatterTransport
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
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.ports import AdapterPort

TransportFactory = Callable[[str], MatterTransport | Awaitable[MatterTransport]]
_SAFE_ENDPOINT = re.compile(r"^wss?://[^/@\s]+(?::\d{1,5})?(?:/[^\s]*)?$")


class MatterConnectionProvider:
    """Provider that validates server schema before exposing an adapter."""

    manifest = ConnectionProviderManifest(
        connector_id="matter",
        display_name="Matter Server",
        protocol="matter",
        version="v1",
        steps=(
            SetupStep(
                step_id="endpoint",
                title="Matter Server endpoint",
                input_keys=("url",),
                fields=(
                    SetupFieldDescriptor(
                        key="url", label="Matter Server URL", input_type=SetupFieldType.URL
                    ),
                ),
            ),
        ),
    )

    def __init__(
        self,
        transport_factory: TransportFactory,
        *,
        default_endpoint: str = "ws://127.0.0.1:5580/ws",
        discovery_timeout: float = 5.0,
    ) -> None:
        if discovery_timeout <= 0:
            raise ValueError("Matter discovery timeout must be positive")
        self._transport_factory = transport_factory
        self.default_endpoint = self.normalize_endpoint(default_endpoint)
        self.discovery_timeout = discovery_timeout

    async def discover(self, endpoint: str | None = None) -> list[ConnectionCandidate]:
        url = self.normalize_endpoint(endpoint or self.default_endpoint)
        return [
            ConnectionCandidate(
                connector_id=self.manifest.connector_id,
                candidate_id=f"matter:{url}",
                display_name=self.manifest.display_name,
                endpoint={"url": url},
                metadata={"discovery": "local_endpoint", "transport": "websocket"},
            )
        ]

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        if candidate.connector_id != self.manifest.connector_id:
            return self._failure("matter_provider_mismatch", "candidate does not belong to Matter")
        try:
            url = self._candidate_url(candidate)
            transport = await self._transport(url)
            connected_before = await transport.health()
            if not connected_before:
                await transport.connect()
            info = self._server_info(transport)
            self._validate_server_info(info)
            nodes = await transport.request("start_listening")
            if not isinstance(nodes, list):
                raise ValueError("Matter Server did not return a node list")
            return ProbeResult(
                transport_status=TransportStatus.CONNECTED,
                inventory_status=InventoryStatus.READY if nodes else InventoryStatus.EMPTY,
                product_status=ProductStatus.CONNECTED,
            )
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            return self._failure("matter_probe_failed", str(error))
        finally:
            if "transport" in locals():
                await transport.disconnect()

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        if setup.candidate.connector_id != self.manifest.connector_id:
            return ValidationResult(
                valid=False,
                diagnostics=(
                    self._diagnostic("matter_provider_mismatch", "setup does not belong to Matter"),
                ),
            )
        if setup.draft.connector_id != self.manifest.connector_id:
            return ValidationResult(
                valid=False,
                diagnostics=(
                    self._diagnostic("matter_provider_mismatch", "draft does not belong to Matter"),
                ),
            )
        try:
            candidate_url = self._candidate_url(setup.candidate)
            draft_url = self.normalize_endpoint(str(setup.draft.endpoint.get("url", "")))
            if candidate_url != draft_url:
                raise ValueError("draft endpoint does not match selected candidate")
            transport = await self._transport(candidate_url)
            if not await transport.health():
                await transport.connect()
            info = self._server_info(transport)
            self._validate_server_info(info)
            nodes = await transport.request("start_listening")
            if not isinstance(nodes, list):
                raise ValueError("Matter Server did not return a node list")
            normalized = setup.draft.model_copy(
                update={
                    "endpoint": {"url": candidate_url},
                    "options": {
                        **setup.draft.options,
                        "server_schema_version": int(info["schema_version"]),
                        "server_implementation": self._implementation(info),
                        "node_count": len(nodes),
                    },
                }
            )
            return ValidationResult(valid=True, normalized_draft=normalized)
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            code = (
                "matter_schema_incompatible"
                if "schema" in str(error).lower()
                else "matter_validation_failed"
            )
            return ValidationResult(valid=False, diagnostics=(self._diagnostic(code, str(error)),))
        finally:
            if "transport" in locals():
                await transport.disconnect()

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort:
        validation = await self.validate(setup)
        if not validation.valid:
            raise ValueError("Matter connection setup is invalid")
        url = self._candidate_url(setup.candidate)
        transport = await self._transport(url)
        return MatterServerAdapter(
            transport,
            adapter_id=adapter_id,
            discovery_timeout=self.discovery_timeout,
        )

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]:
        return (await self.validate(setup)).diagnostics

    @staticmethod
    def normalize_endpoint(endpoint: str) -> str:
        normalized = endpoint.strip()
        if not _SAFE_ENDPOINT.fullmatch(normalized):
            raise ValueError("Matter endpoint must be a ws:// or wss:// URL without credentials")
        return normalized

    async def _transport(self, url: str) -> MatterTransport:
        result = self._transport_factory(url)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _candidate_url(candidate: ConnectionCandidate) -> str:
        value = candidate.endpoint.get("url")
        if not isinstance(value, str):
            raise ValueError("Matter candidate has no endpoint URL")
        return MatterConnectionProvider.normalize_endpoint(value)

    @staticmethod
    def _server_info(transport: MatterTransport) -> dict[str, Any]:
        value = getattr(transport, "server_info", None)
        if not isinstance(value, dict):
            value = getattr(transport, "_server_info", None)
        if not isinstance(value, dict):
            raise ValueError("Matter Server did not expose server information")
        return value

    @staticmethod
    def _validate_server_info(info: dict[str, Any]) -> None:
        schema = info.get("schema_version")
        minimum = info.get("min_supported_schema_version")
        if not isinstance(schema, int) or isinstance(schema, bool) or not isinstance(minimum, int):
            raise ValueError("Matter Server schema range is invalid")
        if not minimum <= 13 <= schema:
            raise ValueError("Matter Server schema range is incompatible")

    @staticmethod
    def _implementation(info: dict[str, Any]) -> str:
        sdk = str(info.get("sdk_version") or "unknown").casefold()
        return "matter.js" if "matter.js" in sdk else "python" if sdk != "unknown" else "unknown"

    @staticmethod
    def _diagnostic(code: str, reason: str) -> ConnectionDiagnostic:
        return ConnectionDiagnostic(
            code=code,
            severity=DiagnosticSeverity.ERROR,
            retryable=True,
            message="Matter Server validation failed",
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


__all__ = ["MatterConnectionProvider"]
