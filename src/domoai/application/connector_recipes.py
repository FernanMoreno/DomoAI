"""Composition and validation helpers for declarative connector recipes."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import cast
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
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import ScalarValue
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.connection_provider_base import ConnectionProviderBase
from domoai.runtime.ports import AdapterPort


class ConnectorRecipeEngineRegistry:
    """Resolve declared recipe engines without performing network I/O."""

    def __init__(
        self,
        *,
        discovery_engines: Mapping[str, object] | None = None,
        transport_engines: Mapping[str, object] | None = None,
    ) -> None:
        self.discovery_engines = dict(discovery_engines or {})
        self.transport_engines = dict(transport_engines or {})

    def validate(self, recipe: ConnectorRecipe) -> tuple[ConnectionDiagnostic, ...]:
        diagnostics: list[ConnectionDiagnostic] = []
        transport_engine = self.transport_engines.get(recipe.transport_family.value)
        if transport_engine is None or not callable(
            getattr(transport_engine, "build_adapter", None)
        ):
            diagnostics.append(
                ConnectionDiagnostic(
                    code="recipe_transport_unsupported",
                    severity=DiagnosticSeverity.ERROR,
                    retryable=False,
                    user_action="Install or implement the declared transport engine",
                    message="No engine is available for this recipe transport",
                )
            )
        if not any(
            (engine := self.discovery_engines.get(strategy.value)) is not None
            and callable(getattr(engine, "discover", None))
            for strategy in recipe.discovery_strategies
            if strategy.value != "manual"
        ):
            if all(strategy.value == "manual" for strategy in recipe.discovery_strategies):
                return tuple(diagnostics)
            diagnostics.append(
                ConnectionDiagnostic(
                    code="recipe_discovery_unsupported",
                    severity=DiagnosticSeverity.ERROR,
                    retryable=False,
                    user_action="Use manual setup or install a discovery engine",
                    message="No engine is available for this recipe discovery strategy",
                )
            )
        return tuple(diagnostics)

    async def discover(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup
    ) -> list[ConnectionCandidate]:
        for strategy in recipe.discovery_strategies:
            engine = self.discovery_engines.get(strategy.value)
            if engine is None:
                continue
            method = getattr(engine, "discover", None)
            if not callable(method):
                continue
            result = method(recipe, setup)
            return list(await result if inspect.isawaitable(result) else result)
        return []

    async def build_adapter(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup, *, adapter_id: str
    ) -> AdapterPort:
        engine = self.transport_engines.get(recipe.transport_family.value)
        if engine is None:
            raise ValueError("recipe transport engine is unavailable")
        method = getattr(engine, "build_adapter", None)
        if not callable(method):
            raise ValueError("recipe transport engine is invalid")
        result = method(recipe, setup, adapter_id=adapter_id)
        resolved = await result if inspect.isawaitable(result) else result
        return cast(AdapterPort, resolved)


class RecipeConnectionProvider(ConnectionProviderBase):
    """Provider implementation that delegates a recipe to registered engines."""

    def __init__(
        self,
        manifest: ConnectionProviderManifest,
        *,
        discovery_engines: Mapping[str, object] | None = None,
        transport_engines: Mapping[str, object] | None = None,
        allowed_hosts: frozenset[str] = frozenset(),
        allowed_networks: tuple[str, ...] = (),
        allowed_ports: frozenset[int] = frozenset(),
        allowed_schemes: frozenset[str] = frozenset(),
        discovery_setup: ConnectionSetup | None = None,
    ) -> None:
        if manifest.recipe is None:
            raise ValueError("recipe provider manifest must include a recipe")
        self.manifest = manifest
        self.recipe = manifest.recipe
        self.engines = ConnectorRecipeEngineRegistry(
            discovery_engines=discovery_engines,
            transport_engines=transport_engines,
        )
        self.allowed_hosts = allowed_hosts
        self.allowed_networks = allowed_networks
        self.allowed_ports = allowed_ports
        self.allowed_schemes = allowed_schemes
        self.discovery_setup = discovery_setup

    async def discover(self) -> list[ConnectionCandidate]:
        if self.discovery_setup is None:
            return []
        return await self.engines.discover(self.recipe, self.discovery_setup)

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        if candidate.connector_id != self.manifest.connector_id:
            return ProbeResult(
                transport_status=TransportStatus.INCOMPATIBLE,
                inventory_status=InventoryStatus.FAILED,
                product_status=ProductStatus.UNAVAILABLE,
                diagnostics=(self._mismatch_diagnostic(),),
            )
        diagnostics = validate_recipe_endpoint(
            candidate.endpoint,
            allowed_hosts=self.allowed_hosts,
            allowed_networks=self.allowed_networks,
            allowed_ports=self.allowed_ports,
            allowed_schemes=self.allowed_schemes,
        )
        if diagnostics:
            return ProbeResult(
                transport_status=TransportStatus.UNREACHABLE,
                inventory_status=InventoryStatus.UNKNOWN,
                product_status=ProductStatus.UNAVAILABLE,
                diagnostics=diagnostics,
            )
        return ProbeResult(
            transport_status=TransportStatus.UNKNOWN,
            inventory_status=InventoryStatus.UNKNOWN,
            product_status=ProductStatus.NEEDS_SETUP,
        )

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        diagnostics = list(self.engines.validate(self.recipe))
        diagnostics.extend(
            validate_recipe_endpoint(
                setup.draft.endpoint,
                allowed_hosts=self.allowed_hosts,
                allowed_networks=self.allowed_networks,
                allowed_ports=self.allowed_ports,
                allowed_schemes=self.allowed_schemes,
            )
        )
        if setup.candidate.connector_id != self.manifest.connector_id:
            diagnostics.append(self._mismatch_diagnostic())
        return ValidationResult(
            valid=not diagnostics,
            diagnostics=tuple(diagnostics),
            normalized_draft=setup.draft if not diagnostics else None,
        )

    async def build_adapter(
        self, setup: ConnectionSetup, *, adapter_id: str
    ) -> AdapterPort:
        validation = await self.validate(setup)
        if not validation.valid:
            raise ValueError("recipe validation failed")
        return await self.engines.build_adapter(self.recipe, setup, adapter_id=adapter_id)

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]:
        return (await self.validate(setup)).diagnostics


def validate_recipe_endpoint(
    endpoint: Mapping[str, ScalarValue],
    *,
    allowed_hosts: frozenset[str],
    allowed_networks: tuple[str, ...] = (),
    allowed_ports: frozenset[int] = frozenset(),
    allowed_schemes: frozenset[str] = frozenset(),
) -> tuple[ConnectionDiagnostic, ...]:
    """Validate endpoint destinations without contacting them."""

    diagnostics: list[ConnectionDiagnostic] = []
    normalized_hosts = {item.strip().casefold() for item in allowed_hosts if item.strip()}
    networks = _parse_allowed_networks(allowed_networks)
    if not normalized_hosts and not networks:
        return (_network_policy_missing_diagnostic(),)

    normalized_schemes = {item.strip().casefold() for item in allowed_schemes if item.strip()}
    for key in ("url", "base_url", "endpoint", "host"):
        value = endpoint.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        raw_value = value.strip()
        if key == "host":
            host = raw_value.casefold()
            port = endpoint.get("port")
            if port is not None:
                _validate_recipe_port(port)
        else:
            try:
                parsed = urlsplit(raw_value)
            except ValueError as error:
                raise ValueError("endpoint URL is malformed") from error
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("credential-bearing endpoint is not allowed")
            host = (parsed.hostname or "").casefold()
            if not host:
                raise ValueError("endpoint URL must contain a host")
            port = parsed.port
            if (
                parsed.scheme
                and normalized_schemes
                and parsed.scheme.casefold() not in normalized_schemes
            ):
                diagnostics.append(_recipe_scheme_not_allowed_diagnostic())
                break
        if allowed_ports and (port is None or port not in allowed_ports):
            diagnostics.append(_recipe_port_not_allowed_diagnostic())
            break
        if not _host_is_allowed(host, normalized_hosts, networks):
            diagnostics.append(
                ConnectionDiagnostic(
                    code="recipe_endpoint_not_allowed",
                    severity=DiagnosticSeverity.ERROR,
                    retryable=False,
                    user_action="Use an endpoint from the deployment allowlist",
                    message="Recipe endpoint host is not allowed",
                )
            )
            break
    return tuple(diagnostics)


def _parse_allowed_networks(
    values: tuple[str, ...],
) -> tuple[IPv4Network | IPv6Network, ...]:
    try:
        return tuple(ip_network(value.strip(), strict=False) for value in values if value.strip())
    except ValueError as error:
        raise ValueError("network allowlist contains an invalid CIDR") from error


def _host_is_allowed(
    host: str,
    allowed_hosts: set[str],
    allowed_networks: tuple[IPv4Network | IPv6Network, ...],
) -> bool:
    if host in allowed_hosts:
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in allowed_networks)


def _validate_recipe_port(value: ScalarValue) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("endpoint port is invalid")


def _network_policy_missing_diagnostic() -> ConnectionDiagnostic:
    return ConnectionDiagnostic(
        code="recipe_network_policy_missing",
        severity=DiagnosticSeverity.ERROR,
        retryable=False,
        user_action="Declare the deployment-owned host or CIDR allowlist",
        message="Recipe endpoint network policy is required",
    )


def _recipe_scheme_not_allowed_diagnostic() -> ConnectionDiagnostic:
    return ConnectionDiagnostic(
        code="recipe_endpoint_scheme_not_allowed",
        severity=DiagnosticSeverity.ERROR,
        retryable=False,
        user_action="Use a protocol declared by the deployment policy",
        message="Recipe endpoint scheme is not allowed",
    )


def _recipe_port_not_allowed_diagnostic() -> ConnectionDiagnostic:
    return ConnectionDiagnostic(
        code="recipe_endpoint_port_not_allowed",
        severity=DiagnosticSeverity.ERROR,
        retryable=False,
        user_action="Use a port declared by the deployment policy",
        message="Recipe endpoint port is not allowed",
    )


def validate_connector_recipe(
    recipe: ConnectorRecipe,
    *,
    discovery_engines: Mapping[str, object] | None = None,
    transport_engines: Mapping[str, object] | None = None,
) -> tuple[ConnectionDiagnostic, ...]:
    """Validate recipe composition without loading secrets or doing network I/O."""

    return ConnectorRecipeEngineRegistry(
        discovery_engines=discovery_engines,
        transport_engines=transport_engines,
    ).validate(recipe)


__all__ = [
    "ConnectorRecipeEngineRegistry",
    "RecipeConnectionProvider",
    "validate_connector_recipe",
    "validate_recipe_endpoint",
]
