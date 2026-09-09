"""Home Assistant connection onboarding over the existing HA adapter."""

from __future__ import annotations

import inspect
import json
import ssl
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
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
from domoai.runtime.connection_ports import (
    ConnectionProviderManifest,
    ConnectionProviderPort,
    ConnectionSetup,
)
from domoai.runtime.ports import AdapterPort
from domoai.security.secrets import SecretResolverPort

ClientFactory = Callable[[str, str], Any]
MdnsDiscoverer = Callable[
    [], Awaitable[Sequence[ConnectionCandidate]] | Sequence[ConnectionCandidate]
]


class HomeAssistantConnectionProvider(ConnectionProviderPort):
    """Discover and validate HA installations without storing raw tokens."""

    manifest = ConnectionProviderManifest(
        connector_id="home_assistant",
        display_name="Home Assistant",
        protocol="home_assistant",
        version="1.0",
        steps=(
            SetupStep(
                step_id="endpoint",
                title="Home Assistant URL",
                input_keys=("url",),
                fields=(
                    SetupFieldDescriptor(
                        key="url",
                        label="Home Assistant URL",
                        input_type=SetupFieldType.URL,
                        help_text="The base URL of the Home Assistant instance.",
                    ),
                ),
            ),
            SetupStep(
                step_id="credentials",
                title="Access token",
                input_keys=("token",),
                secret_input_keys=("token",),
                fields=(
                    SetupFieldDescriptor(
                        key="token",
                        label="Access token",
                        input_type=SetupFieldType.PASSWORD,
                        secret=True,
                        help_text="Stored in the configured secret store.",
                    ),
                ),
            ),
        ),
    )

    def __init__(
        self,
        *,
        secret_resolver: SecretResolverPort,
        client_factory: ClientFactory | None = None,
        mdns_discover: MdnsDiscoverer | None = None,
    ) -> None:
        self.secret_resolver = secret_resolver
        self.client_factory = client_factory or (
            lambda url, token: HomeAssistantClient(url, token)
        )
        self.mdns_discover = mdns_discover

    async def discover(self) -> list[ConnectionCandidate]:
        if self.mdns_discover is None:
            return []
        result = self.mdns_discover()
        if inspect.isawaitable(result):
            result = await result
        candidates = list(result)
        if any(candidate.connector_id != self.manifest.connector_id for candidate in candidates):
            raise ValueError("Home Assistant discovery returned an incompatible candidate")
        return candidates

    def manual_candidate(self, base_url: str) -> ConnectionCandidate:
        normalized = _normalize_url(base_url)
        return ConnectionCandidate(
            connector_id=self.manifest.connector_id,
            candidate_id=f"ha:{normalized}",
            display_name="Home Assistant",
            endpoint={"url": normalized},
            metadata={"source": "manual", "tls": normalized.startswith("https://")},
        )

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        try:
            url = _url_from_setup(candidate.endpoint)
            client = self.client_factory(url, "")
            healthy = await client.health()
            await _close_client(client)
            if not healthy:
                return ProbeResult(
                    transport_status=TransportStatus.AUTH_REQUIRED,
                    inventory_status=InventoryStatus.UNKNOWN,
                    product_status=ProductStatus.REAUTH_REQUIRED,
                    diagnostics=(
                        _diagnostic(
                            "authentication_required",
                            DiagnosticSeverity.WARNING,
                            "Home Assistant requires credentials",
                            retryable=False,
                        ),
                    ),
                )
            return ProbeResult(
                transport_status=TransportStatus.REACHABLE,
                inventory_status=InventoryStatus.UNKNOWN,
                product_status=ProductStatus.NEEDS_SETUP,
            )
        except Exception as error:  # noqa: BLE001 - diagnostics are sanitized
            return ProbeResult(
                transport_status=TransportStatus.UNREACHABLE,
                inventory_status=InventoryStatus.FAILED,
                product_status=ProductStatus.UNAVAILABLE,
                diagnostics=(_exception_diagnostic(error),),
            )

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        try:
            url = _url_from_setup(setup.draft.endpoint)
            if setup.draft.credential_ref is None:
                return ValidationResult(
                    valid=False,
                    diagnostics=(
                        _diagnostic(
                            "credential_required",
                            DiagnosticSeverity.ERROR,
                            "Home Assistant credentials are required",
                            retryable=False,
                        ),
                    ),
                )
            token = _resolve_access_token(self.secret_resolver.resolve(setup.draft.credential_ref))
            client = self.client_factory(url, token)
            if not await client.health():
                return ValidationResult(
                    valid=False,
                    diagnostics=(
                        _diagnostic(
                            "unreachable",
                            DiagnosticSeverity.ERROR,
                            "Home Assistant is unavailable",
                            retryable=True,
                        ),
                    ),
                )
            fetch_registry = getattr(client, "fetch_entity_registry", None)
            if callable(fetch_registry):
                await fetch_registry()
            return ValidationResult(valid=True, normalized_draft=setup.draft)
        except Exception as error:  # noqa: BLE001 - credentials/errors stay at boundary
            return ValidationResult(valid=False, diagnostics=(_exception_diagnostic(error),))
        finally:
            client = locals().get("client")
            if client is not None:
                await _close_client(client)

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort:
        validation = await self.validate(setup)
        if not validation.valid:
            raise ValueError("Home Assistant connection validation failed")
        assert setup.draft.credential_ref is not None
        token = _resolve_access_token(self.secret_resolver.resolve(setup.draft.credential_ref))
        client = self.client_factory(_url_from_setup(setup.draft.endpoint), token)
        return HomeAssistantProviderAdapter(
            HomeAssistantProvider(client),
            adapter_id=adapter_id,
        )

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]:
        return (await self.validate(setup)).diagnostics


def _url_from_setup(endpoint: Mapping[str, object]) -> str:
    value = endpoint.get("url")
    if not isinstance(value, str):
        raise ValueError("Home Assistant URL is required")
    return _normalize_url(value)


def _normalize_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("Home Assistant URL is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Home Assistant URL must not contain credentials")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _diagnostic(
    code: str,
    severity: DiagnosticSeverity,
    message: str,
    *,
    retryable: bool,
) -> ConnectionDiagnostic:
    return ConnectionDiagnostic(
        code=code,
        severity=severity,
        retryable=retryable,
        message=message,
    )


def _exception_diagnostic(error: Exception) -> ConnectionDiagnostic:
    if isinstance(error, PermissionError):
        return _diagnostic(
            "authentication_failed",
            DiagnosticSeverity.ERROR,
            "Home Assistant authentication failed",
            retryable=False,
        )
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return _diagnostic(
            "timeout", DiagnosticSeverity.ERROR, "Home Assistant probe timed out", retryable=True
        )
    if isinstance(error, ssl.SSLError):
        return _diagnostic(
            "tls_certificate_invalid",
            DiagnosticSeverity.ERROR,
            "Home Assistant TLS validation failed",
            retryable=False,
        )
    if isinstance(error, ValueError):
        return _diagnostic(
            "incompatible",
            DiagnosticSeverity.ERROR,
            "Home Assistant returned an incompatible response",
            retryable=False,
        )
    return _diagnostic(
        "unreachable", DiagnosticSeverity.ERROR, "Home Assistant is unavailable", retryable=True
    )


async def _close_client(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


def _resolve_access_token(value: str) -> str:
    """Accept legacy raw tokens and the versioned encrypted OAuth bundle."""

    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(payload, dict) or "access_token" not in payload:
        return value
    token = payload.get("access_token")
    if payload.get("schema_version") != "v1" or not isinstance(token, str) or not token:
        raise ValueError("Home Assistant OAuth credential is invalid")
    return token


__all__ = ["HomeAssistantConnectionProvider"]
