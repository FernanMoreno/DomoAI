"""Explicit and opt-in entry-point registration for adapter packages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from typing import Any, cast

from domoai.domain.models import AdapterSnapshot
from domoai.runtime.ports import AdapterPort

from .manifest import (
    ADAPTER_ENTRY_POINT_GROUP,
    AdapterManifest,
    CapabilityCompatibility,
    CapabilityCompatibilityStatus,
    CompatibilityDiagnostic,
    CompatibilityReport,
    CompatibilityStatus,
    DiagnosticSeverity,
    sanitize_exception,
)

type AdapterFactory = Callable[[], AdapterPort]

_REQUIRED_ADAPTER_METHODS = (
    "connect",
    "disconnect",
    "discover",
    "read_state",
    "execute",
    "subscribe_events",
    "health",
)


class DuplicateAdapterError(ValueError):
    """Raised when two packages claim the same stable adapter id."""


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    manifest: AdapterManifest
    factory: AdapterFactory

    def create(self) -> AdapterPort:
        adapter = self.factory()
        actual_id = getattr(adapter, "adapter_id", None)
        if actual_id != self.manifest.adapter_id:
            raise ValueError(
                "adapter factory returned an incompatible adapter_id "
                f"for {self.manifest.adapter_id!r}"
            )
        missing = tuple(
            name for name in _REQUIRED_ADAPTER_METHODS if not callable(getattr(adapter, name, None))
        )
        if missing:
            raise TypeError(
                f"adapter {self.manifest.adapter_id!r} is missing required methods: "
                + ", ".join(missing)
            )
        return adapter


class AdapterRegistry:
    """Registry for trusted, explicitly selected adapter registrations."""

    def __init__(self) -> None:
        self._registrations: dict[str, AdapterRegistration] = {}
        self._diagnostics: list[CompatibilityDiagnostic] = []

    @property
    def registrations(self) -> tuple[AdapterRegistration, ...]:
        return tuple(self._registrations[key] for key in sorted(self._registrations))

    @property
    def diagnostics(self) -> tuple[CompatibilityDiagnostic, ...]:
        return tuple(self._diagnostics)

    def register(self, registration: AdapterRegistration) -> None:
        adapter_id = registration.manifest.adapter_id
        if adapter_id in self._registrations:
            raise DuplicateAdapterError(f"adapter id {adapter_id!r} is already registered")
        self._registrations[adapter_id] = registration

    def get(self, adapter_id: str) -> AdapterRegistration | None:
        return self._registrations.get(adapter_id)

    def create(self, adapter_id: str) -> AdapterPort:
        registration = self._registrations.get(adapter_id)
        if registration is None:
            raise KeyError(f"unknown adapter id {adapter_id!r}")
        return registration.create()

    @classmethod
    def from_entry_points(cls, entry_points: Iterable[Any] | None = None) -> AdapterRegistry:
        """Load only selected package entry points; never installs packages."""

        registry = cls()
        points = list(entry_points) if entry_points is not None else _installed_entry_points()
        for entry_point in points:
            group = getattr(entry_point, "group", ADAPTER_ENTRY_POINT_GROUP)
            if group != ADAPTER_ENTRY_POINT_GROUP:
                continue
            name = str(getattr(entry_point, "name", "unknown"))
            try:
                provider = entry_point.load()
                candidate = provider() if callable(provider) else provider
                if not isinstance(candidate, AdapterRegistration):
                    raise TypeError("entry point provider did not return AdapterRegistration")
                registry.register(candidate)
            except DuplicateAdapterError:
                raise
            except Exception as error:
                registry._diagnostics.append(
                    CompatibilityDiagnostic(
                        code="entry_point_load_failed",
                        severity=DiagnosticSeverity.ERROR,
                        subject=name,
                        message=(
                            f"entry point {name!r} could not be loaded "
                            f"({sanitize_exception(error)})"
                        ),
                    )
                )
        return registry

    def compatibility(
        self, adapter_id: str, snapshot: AdapterSnapshot | None = None
    ) -> CompatibilityReport:
        registration = self._registrations.get(adapter_id)
        if registration is None:
            raise KeyError(f"unknown adapter id {adapter_id!r}")
        manifest = registration.manifest
        observed = _observed_capabilities(snapshot) if snapshot is not None else set()
        capabilities: list[CapabilityCompatibility] = []
        diagnostics: list[CompatibilityDiagnostic] = []
        for declaration in manifest.capabilities:
            if snapshot is None or declaration.name in observed:
                capability_status = CapabilityCompatibilityStatus.SUPPORTED
            elif declaration.optional:
                capability_status = CapabilityCompatibilityStatus.OPTIONAL
            else:
                capability_status = CapabilityCompatibilityStatus.UNSUPPORTED
                diagnostics.append(
                    CompatibilityDiagnostic(
                        code="required_capability_missing",
                        severity=DiagnosticSeverity.ERROR,
                        subject=declaration.name,
                        message="required capability was not observed in the fixture snapshot",
                    )
                )
            capabilities.append(
                CapabilityCompatibility(
                    name=declaration.name,
                    status=capability_status,
                    optional=declaration.optional,
                    commands=list(declaration.commands),
                )
            )
        status: CompatibilityStatus = (
            CompatibilityStatus.DEGRADED
            if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics)
            else CompatibilityStatus.COMPATIBLE
        )
        return CompatibilityReport(
            adapter_id=adapter_id,
            status=status,
            capabilities=capabilities,
            diagnostics=diagnostics,
        )


def _installed_entry_points() -> list[Any]:
    try:
        return list(metadata.entry_points(group=ADAPTER_ENTRY_POINT_GROUP))
    except TypeError:
        discovered = metadata.entry_points()
        selected = getattr(discovered, "select", None)
        if callable(selected):
            return list(selected(group=ADAPTER_ENTRY_POINT_GROUP))
        return [
            point
            for point in discovered
            if getattr(point, "group", None) == ADAPTER_ENTRY_POINT_GROUP
        ]


def _observed_capabilities(snapshot: AdapterSnapshot | None) -> set[str]:
    if snapshot is None:
        return set()
    observed: set[str] = set()
    for entity in snapshot.source_entities:
        raw_capabilities = entity.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            continue
        for capability in raw_capabilities:
            if isinstance(capability, dict) and isinstance(capability.get("name"), str):
                observed.add(cast(str, capability["name"]))
    return observed
