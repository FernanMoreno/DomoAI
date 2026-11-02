"""Safe defaults for third-party connection providers.

The base keeps the five-provider contract visible while making a freshly
scaffolded connector honest: it can be loaded and catalogued, but cannot claim
that it validates or builds a real adapter until the author implements it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from urllib.parse import urlsplit

from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDiagnostic,
    DiagnosticSeverity,
    InventoryStatus,
    ProbeResult,
    ProductStatus,
    TransportStatus,
    ValidationResult,
)
from domoai.domain.models import ScalarValue
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.ports import AdapterPort


class ConnectionProviderIncompleteError(RuntimeError):
    """Raised when a generated provider has not implemented its protocol."""

    code = "provider_adapter_not_implemented"


class ConnectionProviderBase:
    """Minimal implementation of all five provider extension points."""

    manifest: ConnectionProviderManifest

    def manual_candidate(
        self,
        endpoint: Mapping[str, ScalarValue],
        *,
        candidate_id: str | None = None,
        display_name: str | None = None,
    ) -> ConnectionCandidate:
        """Create a deterministic manual candidate from secret-free endpoint data."""

        endpoint_data = dict(endpoint)
        for key, value in endpoint_data.items():
            if isinstance(value, str) and not value.strip():
                raise ValueError("manual endpoint values must not be blank")
            if key in {"url", "base_url", "endpoint"} and isinstance(value, str):
                parsed = urlsplit(value.strip())
                if not parsed.scheme or not parsed.netloc:
                    raise ValueError("manual endpoint URL is invalid")
        stable = json.dumps(endpoint_data, sort_keys=True, separators=(",", ":"))
        selected_id = candidate_id or (
            f"{self.manifest.connector_id}:manual:{hashlib.sha256(stable.encode()).hexdigest()[:16]}"
        )
        return ConnectionCandidate(
            connector_id=self.manifest.connector_id,
            candidate_id=selected_id,
            display_name=display_name or self.manifest.display_name,
            endpoint=endpoint_data,
            metadata={"source": "manual"},
        )

    async def discover(self) -> list[ConnectionCandidate]:
        return []

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        if candidate.connector_id != self.manifest.connector_id:
            return ProbeResult(
                transport_status=TransportStatus.INCOMPATIBLE,
                inventory_status=InventoryStatus.FAILED,
                product_status=ProductStatus.UNAVAILABLE,
                diagnostics=(self._mismatch_diagnostic(),),
            )
        return ProbeResult(
            transport_status=TransportStatus.UNKNOWN,
            inventory_status=InventoryStatus.UNKNOWN,
            product_status=ProductStatus.NEEDS_SETUP,
            diagnostics=(self._incomplete_diagnostic(),),
        )

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        if setup.draft.connector_id != self.manifest.connector_id:
            return ValidationResult(valid=False, diagnostics=(self._mismatch_diagnostic(),))
        return ValidationResult(valid=False, diagnostics=(self._incomplete_diagnostic(),))

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort:
        del setup, adapter_id
        raise ConnectionProviderIncompleteError(
            "provider adapter implementation is required before activation"
        )

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]:
        return (await self.validate(setup)).diagnostics

    def _incomplete_diagnostic(self) -> ConnectionDiagnostic:
        return ConnectionDiagnostic(
            code=ConnectionProviderIncompleteError.code,
            severity=DiagnosticSeverity.ERROR,
            retryable=False,
            user_action="Implement the provider protocol adapter",
            message="This connector scaffold still needs its provider adapter",
        )

    @staticmethod
    def _mismatch_diagnostic() -> ConnectionDiagnostic:
        return ConnectionDiagnostic(
            code="provider_mismatch",
            severity=DiagnosticSeverity.ERROR,
            retryable=False,
            message="candidate does not belong to this provider",
        )


__all__ = ["ConnectionProviderBase", "ConnectionProviderIncompleteError"]
