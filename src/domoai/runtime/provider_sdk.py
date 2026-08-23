"""Provider SDK protocols and deterministic in-process composition."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Protocol, cast

from domoai.domain.models import ExecutionStatus
from domoai.domain.provider import (
    DeviceDescriptor,
    Measurement,
    ProviderCollectionResult,
    ProviderCommand,
    ProviderDiagnostic,
    ProviderDiscoveryResult,
    ProviderExecutionResult,
    ProviderManifest,
    ProviderRole,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext


class TelemetryProviderPort(Protocol):
    manifest: ProviderManifest

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def discover(self) -> list[DeviceDescriptor]: ...

    async def get_measurements(
        self, device_ids: Sequence[str] | None = None
    ) -> list[Measurement]: ...

    def subscribe(self) -> AsyncIterator[Measurement]: ...


class CommandProviderPort(Protocol):
    manifest: ProviderManifest

    async def execute(
        self,
        command: ProviderCommand,
        execution_context: ExecutionContext | None = None,
    ) -> ProviderExecutionResult: ...


class ProviderRegistryError(ValueError):
    """Raised when a provider cannot be safely registered or routed."""


class DuplicateProviderError(ProviderRegistryError):
    """Raised when two providers claim the same stable provider ID."""


_TELEMETRY_METHODS = (
    "connect",
    "disconnect",
    "discover",
    "get_measurements",
    "subscribe",
)


class ProviderRegistry:
    """Register providers and compose them without leaking provider failures."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._providers: dict[str, object] = {}
        self._clock = clock or SystemClock()

    @property
    def providers(self) -> tuple[object, ...]:
        """Return providers in stable provider-ID order."""

        return tuple(self._providers[provider_id] for provider_id in sorted(self._providers))

    def register(self, provider: object) -> None:
        manifest = _manifest_for(provider)
        if manifest.provider_id in self._providers:
            raise DuplicateProviderError(
                f"provider id {manifest.provider_id!r} is already registered"
            )
        _validate_role_methods(provider, manifest)
        self._providers[manifest.provider_id] = provider

    def get(self, provider_id: str) -> object | None:
        return self._providers.get(provider_id)

    async def discover_all(self) -> ProviderDiscoveryResult:
        devices: list[DeviceDescriptor] = []
        diagnostics: list[ProviderDiagnostic] = []
        for provider in self.providers:
            manifest = _manifest_for(provider)
            if ProviderRole.TELEMETRY not in manifest.roles:
                continue
            try:
                discovered = await _call_async(provider, "discover")
                if not isinstance(discovered, list) or not all(
                    isinstance(item, DeviceDescriptor) for item in discovered
                ):
                    raise TypeError("provider returned invalid device descriptors")
                if any(item.provider_id != manifest.provider_id for item in discovered):
                    raise ValueError("device descriptor provenance does not match provider")
                devices.extend(discovered)
            except Exception as error:
                diagnostics.append(_diagnostic(manifest.provider_id, "discovery_failed", error))
        return ProviderDiscoveryResult(devices=devices, diagnostics=diagnostics)

    async def collect(self, device_ids: Sequence[str] | None = None) -> ProviderCollectionResult:
        measurements: list[Measurement] = []
        diagnostics: list[ProviderDiagnostic] = []
        for provider in self.providers:
            manifest = _manifest_for(provider)
            if ProviderRole.TELEMETRY not in manifest.roles:
                continue
            try:
                collected = await _call_async(provider, "get_measurements", device_ids)
                if not isinstance(collected, list) or not all(
                    isinstance(item, Measurement) for item in collected
                ):
                    raise TypeError("provider returned invalid measurements")
                if any(item.provider_id != manifest.provider_id for item in collected):
                    raise ValueError("measurement provenance does not match provider")
                measurements.extend(collected)
            except Exception as error:
                diagnostics.append(_diagnostic(manifest.provider_id, "collection_failed", error))
        return ProviderCollectionResult(measurements=measurements, diagnostics=diagnostics)

    async def execute(
        self,
        provider_id: str,
        command: ProviderCommand,
        execution_context: ExecutionContext | None = None,
    ) -> ProviderExecutionResult:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ProviderRegistryError(f"unknown provider id {provider_id!r}")
        manifest = _manifest_for(provider)
        if ProviderRole.COMMANDS not in manifest.roles:
            raise ProviderRegistryError(f"provider {provider_id!r} does not support commands")
        if command.provider_id != provider_id:
            raise ProviderRegistryError("command provider_id does not match route")
        try:
            if execution_context is None:
                result = await _call_async(provider, "execute", command)
            else:
                result = await _call_async(provider, "execute", command, execution_context)
        except Exception:
            return ProviderExecutionResult(
                provider_id=provider_id,
                external_device_id=command.external_device_id,
                command=command.command,
                status=ExecutionStatus.FAILED,
                completed_at=self._clock.now(),
                message="provider command failed",
            )
        if not isinstance(result, ProviderExecutionResult):
            return ProviderExecutionResult(
                provider_id=provider_id,
                external_device_id=command.external_device_id,
                command=command.command,
                status=ExecutionStatus.FAILED,
                completed_at=self._clock.now(),
                message="provider returned an invalid command result",
            )
        if result.provider_id != provider_id:
            return ProviderExecutionResult(
                provider_id=provider_id,
                external_device_id=command.external_device_id,
                command=command.command,
                status=ExecutionStatus.FAILED,
                completed_at=self._clock.now(),
                message="provider returned an invalid command provenance",
            )
        return result


def _manifest_for(provider: object) -> ProviderManifest:
    manifest = getattr(provider, "manifest", None)
    if not isinstance(manifest, ProviderManifest):
        raise TypeError("provider must expose a valid ProviderManifest")
    return manifest


def _validate_role_methods(provider: object, manifest: ProviderManifest) -> None:
    if ProviderRole.TELEMETRY in manifest.roles:
        missing = tuple(
            method for method in _TELEMETRY_METHODS if not callable(getattr(provider, method, None))
        )
        if missing:
            raise TypeError(
                f"telemetry provider {manifest.provider_id!r} is missing required methods: "
                + ", ".join(missing)
            )
    if ProviderRole.COMMANDS in manifest.roles and not callable(getattr(provider, "execute", None)):
        raise TypeError(f"command provider {manifest.provider_id!r} is missing execute")


async def _call_async(provider: object, method_name: str, *args: object) -> object:
    method = getattr(provider, method_name, None)
    if not callable(method):
        raise TypeError(f"provider method {method_name!r} is unavailable")
    callable_method = cast(Callable[..., Any], method)
    return await callable_method(*args)


def _diagnostic(provider_id: str, code: str, error: BaseException) -> ProviderDiagnostic:
    return ProviderDiagnostic(
        code=code,
        provider_id=provider_id,
        message="provider operation failed",
        retryable=isinstance(error, (ConnectionError, TimeoutError, OSError)),
    )
