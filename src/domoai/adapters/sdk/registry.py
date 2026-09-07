"""Explicit and opt-in entry-point registration for adapter packages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from typing import Any, cast

from domoai.domain.models import AdapterSnapshot, Capability
from domoai.runtime.ports import AdapterPort

from .manifest import (
    ADAPTER_ENTRY_POINT_GROUP,
    AdapterManifest,
    CapabilityCompatibility,
    CapabilityCompatibilityStatus,
    CapabilityDeclaration,
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
        observed_details = (
            _observed_capability_details(snapshot) if snapshot is not None else {}
        )
        capabilities: list[CapabilityCompatibility] = []
        diagnostics: list[CompatibilityDiagnostic] = []
        for name in sorted(_malformed_observed_capabilities(snapshot)):
            diagnostics.append(
                CompatibilityDiagnostic(
                    code="malformed_observed_capability",
                    severity=DiagnosticSeverity.ERROR,
                    subject=name,
                    message="observed capability does not satisfy the canonical contract",
                )
            )
        for declaration in manifest.capabilities:
            if snapshot is None or declaration.name in observed:
                capability_status = CapabilityCompatibilityStatus.SUPPORTED
                diagnostics.extend(
                    _compare_capability_guarantees(
                        declaration,
                        observed_details.get(declaration.name, ()),
                    )
                )
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


def _observed_capability_details(
    snapshot: AdapterSnapshot,
) -> dict[str, list[Capability]]:
    details: dict[str, list[Capability]] = {}
    for entity in snapshot.source_entities:
        raw_capabilities = entity.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            continue
        for raw_capability in raw_capabilities:
            if not isinstance(raw_capability, dict):
                continue
            name = raw_capability.get("name")
            if not isinstance(name, str):
                continue
            try:
                capability = Capability.model_validate(raw_capability)
            except Exception:
                # The existing discovery compatibility report will still show
                # the capability as observed; this separate diagnostic keeps
                # malformed details fail-closed without exposing provider data.
                continue
            details.setdefault(name, []).append(capability)
    return details


def _malformed_observed_capabilities(snapshot: AdapterSnapshot | None) -> set[str]:
    if snapshot is None:
        return set()
    malformed: set[str] = set()
    for entity in snapshot.source_entities:
        raw_capabilities = entity.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            continue
        for raw_capability in raw_capabilities:
            if not isinstance(raw_capability, dict):
                continue
            name = raw_capability.get("name")
            if not isinstance(name, str):
                malformed.add("unknown")
                continue
            try:
                Capability.model_validate(raw_capability)
            except Exception:
                malformed.add(name)
    return malformed


def _compare_capability_guarantees(
    declaration: CapabilityDeclaration,
    observed: tuple[Capability, ...] | list[Capability],
) -> list[CompatibilityDiagnostic]:
    diagnostics: list[CompatibilityDiagnostic] = []
    for capability in observed:
        violations: list[str] = []
        if capability.writable and not declaration.writable:
            violations.append("writable_scope")
        if (
            declaration.minimum is not None
            and capability.minimum is not None
            and capability.minimum < declaration.minimum
        ):
            violations.append("minimum_range")
        if (
            declaration.maximum is not None
            and capability.maximum is not None
            and capability.maximum > declaration.maximum
        ):
            violations.append("maximum_range")
        if declaration.guarantees.readback_required and not capability.guarantees.readback_required:
            violations.append("readback_required")
        declared_latency = declaration.guarantees.expected_latency_ms
        observed_latency = capability.guarantees.expected_latency_ms
        if (
            declared_latency is not None
            and observed_latency is not None
            and observed_latency < declared_latency
        ):
            violations.append("expected_latency_ms")
        if violations:
            diagnostics.append(
                CompatibilityDiagnostic(
                    code="capability_guarantee_mismatch",
                    severity=DiagnosticSeverity.ERROR,
                    subject=declaration.name,
                    message="observed capability exceeds its provider declaration",
                    details={"violations": ",".join(violations)},
                )
            )
    return diagnostics
