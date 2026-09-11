"""Onboarding contracts for connection providers.

This boundary discovers and validates installations. It does not replace the
runtime AdapterPort or expose vendor operations to agent-facing callers.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDiagnostic,
    ConnectionDraft,
    DiagnosticSeverity,
    InventoryStatus,
    ProbeResult,
    ProductStatus,
    ProviderCatalogEntry,
    TransportStatus,
    ValidationResult,
)
from domoai.runtime.connection_ports import (
    ConnectionProviderManifest,
    ConnectionProviderPort,
    ConnectionSetup,
)
from domoai.runtime.connection_provider_loader import (
    ConnectionProviderLoadDiagnostic,
    ConnectionProviderLoader,
)
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics
from domoai.runtime.ports import AdapterPort


class ConnectionProviderRegistry:
    """Deterministic registry for onboarding providers."""

    def __init__(self, *, operational_metrics: RuntimeOperationalMetrics | None = None) -> None:
        self._providers: dict[str, ConnectionProviderPort] = {}
        self._diagnostics: list[ConnectionProviderLoadDiagnostic] = []
        self.operational_metrics = operational_metrics

    def register(self, provider: ConnectionProviderPort) -> None:
        manifest = provider.manifest
        if manifest.contract_version != "v1":
            raise ValueError("unsupported connection provider contract")
        if manifest.connector_id in self._providers:
            raise ValueError(f"connection provider {manifest.connector_id!r} already registered")
        self._providers[manifest.connector_id] = provider

    def get(self, connector_id: str) -> ConnectionProviderPort | None:
        return self._providers.get(connector_id)

    @property
    def providers(self) -> tuple[ConnectionProviderPort, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    @property
    def catalog(self) -> tuple[ProviderCatalogEntry, ...]:
        """Expose manifest metadata without exposing provider instances."""

        return tuple(
            ProviderCatalogEntry.from_manifest(provider.manifest) for provider in self.providers
        )

    @property
    def diagnostics(self) -> tuple[ConnectionProviderLoadDiagnostic, ...]:
        return tuple(self._diagnostics)

    @classmethod
    def from_entry_points(
        cls,
        entry_points: Iterable[object] | None = None,
        *,
        builtins: tuple[ConnectionProviderPort, ...] = (),
        operational_metrics: RuntimeOperationalMetrics | None = None,
    ) -> ConnectionProviderRegistry:
        """Build a registry while isolating external package failures."""

        registry = cls(operational_metrics=operational_metrics)
        for provider in builtins:
            registry.register(provider)
        result = ConnectionProviderLoader.load(entry_points)
        registry._diagnostics.extend(result.diagnostics)
        for provider in result.providers:
            try:
                registry.register(provider)
            except ValueError:
                registry._diagnostics.append(
                    ConnectionProviderLoadDiagnostic(
                        code="provider_duplicate",
                        entry_point=provider.manifest.connector_id,
                        message="connection provider entry point was rejected",
                    )
                )
        return registry


class OnboardingCoordinator:
    """Bounded discover/select/setup/validate/build session coordinator."""

    def __init__(
        self,
        providers: ConnectionProviderRegistry,
        *,
        ttl_seconds: int = 900,
        max_sessions: int = 100,
        close_session: Callable[[UUID], None] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("onboarding session TTL must be positive")
        if max_sessions <= 0:
            raise ValueError("onboarding session capacity must be positive")
        self.providers = providers
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_sessions = max_sessions
        self.close_session = close_session
        self._sessions: dict[UUID, dict[str, object]] = {}

    async def start(self, connector_id: str) -> tuple[UUID, tuple[ConnectionCandidate, ...]]:
        provider = self._provider(connector_id)
        started = time.perf_counter()
        try:
            candidates = tuple(await provider.discover())
        except Exception:
            if self.providers.operational_metrics is not None:
                self.providers.operational_metrics.record_connection_state(
                    "unavailable", connector_id=connector_id
                )
            raise
        if self.providers.operational_metrics is not None:
            self.providers.operational_metrics.record_connection_discovery_duration(
                time.perf_counter() - started, connector_id=connector_id
            )
            self.providers.operational_metrics.record_connection_total(connector_id)
        now = datetime.now(UTC)
        self._purge_expired(now)
        while len(self._sessions) >= self.max_sessions:
            oldest_id = min(
                self._sessions,
                key=lambda session_id: cast(datetime, self._sessions[session_id]["expires_at"]),
            )
            self.cancel(oldest_id)
        session_id = uuid4()
        self._sessions[session_id] = {
            "connector_id": connector_id,
            "candidates": candidates,
            "candidate": None,
            "setup": None,
            "expires_at": now + self.ttl,
        }
        return session_id, candidates

    async def select(self, session_id: UUID, candidate_id: str) -> ConnectionCandidate:
        session = self._session(session_id)
        candidates = cast(tuple[ConnectionCandidate, ...], session["candidates"])
        candidate = next((item for item in candidates if item.candidate_id == candidate_id), None)
        if candidate is None:
            raise ValueError("onboarding candidate was not found")
        session["candidate"] = candidate
        return candidate

    def cancel(self, session_id: UUID) -> bool:
        existed = self._sessions.pop(session_id, None) is not None
        if self.close_session is not None:
            self.close_session(session_id)
        return existed

    async def setup(self, session_id: UUID, draft: ConnectionDraft) -> ConnectionSetup:
        session = self._session(session_id)
        candidate = session.get("candidate")
        if not isinstance(candidate, ConnectionCandidate):
            raise ValueError("onboarding candidate must be selected")
        setup = ConnectionSetup(candidate=candidate, draft=draft)
        session["setup"] = setup
        return setup

    async def get_candidate(self, session_id: UUID) -> ConnectionCandidate:
        """Return the selected candidate without exposing session internals."""

        session = self._session(session_id)
        candidate = session.get("candidate")
        if not isinstance(candidate, ConnectionCandidate):
            raise ValueError("onboarding candidate must be selected")
        return candidate

    async def validate(self, session_id: UUID) -> ValidationResult:
        session = self._session(session_id)
        setup = session.get("setup")
        if not isinstance(setup, ConnectionSetup):
            raise ValueError("onboarding setup is required")
        started = time.perf_counter()
        connector_id = setup.draft.connector_id
        try:
            result = await self._provider(connector_id).validate(setup)
        except Exception:
            if self.providers.operational_metrics is not None:
                self.providers.operational_metrics.record_connection_state(
                    "unavailable", connector_id=connector_id
                )
            raise
        if self.providers.operational_metrics is not None:
            self.providers.operational_metrics.record_connection_validation_duration(
                time.perf_counter() - started, connector_id=connector_id
            )
            self.providers.operational_metrics.record_connection_state(
                "connected" if result.valid else "degraded", connector_id=connector_id
            )
        return result

    async def build(self, session_id: UUID, *, adapter_id: str) -> AdapterPort:
        session = self._session(session_id)
        setup = session.get("setup")
        if not isinstance(setup, ConnectionSetup):
            raise ValueError("onboarding setup is required")
        validation = await self._provider(setup.draft.connector_id).validate(setup)
        if not validation.valid:
            raise ValueError("onboarding validation failed")
        return await self._provider(setup.draft.connector_id).build_adapter(
            setup, adapter_id=adapter_id
        )

    async def get_setup(self, session_id: UUID) -> ConnectionSetup:
        """Return the current secret-safe setup for the application service."""

        session = self._session(session_id)
        setup = session.get("setup")
        if not isinstance(setup, ConnectionSetup):
            raise ValueError("onboarding setup is required")
        return setup

    def _provider(self, connector_id: str) -> ConnectionProviderPort:
        provider = self.providers.get(connector_id)
        if provider is None:
            raise ValueError("connection provider is unavailable")
        return provider

    def _session(self, session_id: UUID) -> dict[str, object]:
        session = self._sessions.get(session_id)
        if session is None or cast(datetime, session["expires_at"]) <= datetime.now(UTC):
            self.cancel(session_id)
            raise ValueError("onboarding session is expired")
        return session

    def _purge_expired(self, now: datetime) -> None:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if cast(datetime, session["expires_at"]) <= now
        ]
        for session_id in expired:
            self.cancel(session_id)


class FixtureConnectionProvider:
    """Hardware-independent provider used by control-plane tests and demos."""

    def __init__(
        self,
        manifest: ConnectionProviderManifest,
        *,
        adapter_factory: Callable[[], AdapterPort],
    ) -> None:
        self.manifest = manifest
        self._adapter_factory = adapter_factory

    async def discover(self) -> list[ConnectionCandidate]:
        return [
            ConnectionCandidate(
                connector_id=self.manifest.connector_id,
                candidate_id="fixture:default",
                display_name=self.manifest.display_name,
                endpoint={"host": "fixture"},
                metadata={"discovery": "fixture"},
            )
        ]

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        if candidate.connector_id != self.manifest.connector_id:
            return ProbeResult(
                transport_status=TransportStatus.INCOMPATIBLE,
                inventory_status=InventoryStatus.FAILED,
                product_status=ProductStatus.UNAVAILABLE,
                diagnostics=(
                    ConnectionDiagnostic(
                        code="provider_mismatch",
                        severity=DiagnosticSeverity.ERROR,
                        retryable=False,
                        message="candidate does not belong to this provider",
                    ),
                ),
            )
        return ProbeResult(
            transport_status=TransportStatus.REACHABLE,
            inventory_status=InventoryStatus.READY,
            product_status=ProductStatus.CONNECTED,
        )

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        if setup.candidate.connector_id != self.manifest.connector_id:
            return ValidationResult(
                valid=False,
                diagnostics=(
                    ConnectionDiagnostic(
                        code="provider_mismatch",
                        severity=DiagnosticSeverity.ERROR,
                        retryable=False,
                        message="setup does not belong to this provider",
                    ),
                ),
            )
        return ValidationResult(valid=True, normalized_draft=setup.draft)

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort:
        validation = await self.validate(setup)
        if not validation.valid:
            raise ValueError("connection setup is invalid")
        result = self._adapter_factory()
        if inspect.isawaitable(result):
            result = await cast(Awaitable[AdapterPort], result)
        try:
            result.adapter_id = adapter_id
        except (AttributeError, TypeError):
            if getattr(result, "adapter_id", None) != adapter_id:
                raise ValueError("fixture adapter cannot adopt the runtime identity") from None
        return result

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]:
        validation = await self.validate(setup)
        return validation.diagnostics


__all__ = [
    "ConnectionProviderManifest",
    "ConnectionProviderPort",
    "ConnectionProviderRegistry",
    "ConnectionSetup",
    "FixtureConnectionProvider",
    "OnboardingCoordinator",
]
