"""Best-effort entry-point loader for the public Connection Provider SDK."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata
from typing import Any

from pydantic import Field

from domoai.domain.models import StrictModel
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionProviderPort

CONNECTION_PROVIDER_ENTRY_POINT_GROUP = "domoai.connection_providers"
_METHODS = ("discover", "probe", "validate", "build_adapter", "diagnose")
_SENSITIVE_TEXT = ("token=", "password=", "secret=", "authorization=", "private_key=")


class ConnectionProviderLoadDiagnostic(StrictModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    entry_point: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=240)


@dataclass(frozen=True)
class ConnectionProviderLoadResult:
    providers: tuple[ConnectionProviderPort, ...]
    diagnostics: tuple[ConnectionProviderLoadDiagnostic, ...]


class ConnectionProviderLoader:
    """Load providers independently; one broken package never blocks boot."""

    @classmethod
    def load(cls, entry_points: Iterable[Any] | None = None) -> ConnectionProviderLoadResult:
        points = list(entry_points) if entry_points is not None else cls._installed()
        providers: list[ConnectionProviderPort] = []
        diagnostics: list[ConnectionProviderLoadDiagnostic] = []
        seen: set[str] = set()
        for point in points:
            group = getattr(point, "group", CONNECTION_PROVIDER_ENTRY_POINT_GROUP)
            if group != CONNECTION_PROVIDER_ENTRY_POINT_GROUP:
                continue
            name = str(getattr(point, "name", "unknown"))[:128]
            try:
                try:
                    loaded = point.load()
                except Exception as error:
                    raise _ProviderLoadError("provider_entry_point_load_failed") from error
                candidate = loaded() if callable(loaded) else loaded
                cls._validate(candidate)
                connector_id = candidate.manifest.connector_id
                if connector_id in seen:
                    raise _ProviderLoadError("provider_duplicate")
                seen.add(connector_id)
                providers.append(candidate)
            except _ProviderLoadError as error:
                diagnostics.append(cls._diagnostic(str(error), name))
            except Exception:
                diagnostics.append(cls._diagnostic("provider_entry_point_invalid", name))
        return ConnectionProviderLoadResult(tuple(providers), tuple(diagnostics))

    @staticmethod
    def _validate(provider: object) -> None:
        manifest = getattr(provider, "manifest", None)
        if not isinstance(manifest, ConnectionProviderManifest):
            raise _ProviderLoadError("provider_entry_point_invalid")
        if manifest.contract_version != "v1":
            raise _ProviderLoadError("provider_contract_unsupported")
        for value in (
            manifest.connector_id,
            manifest.display_name,
            manifest.protocol,
            manifest.version,
        ):
            if any(marker in value.casefold() for marker in _SENSITIVE_TEXT):
                raise _ProviderLoadError("provider_metadata_sensitive")
        for method_name in _METHODS:
            method = getattr(provider, method_name, None)
            if not callable(method) or not inspect.iscoroutinefunction(method):
                raise _ProviderLoadError("provider_entry_point_invalid")

    @staticmethod
    def _diagnostic(code: str, entry_point: str) -> ConnectionProviderLoadDiagnostic:
        known_codes = {
            "provider_duplicate",
            "provider_entry_point_invalid",
            "provider_entry_point_load_failed",
            "provider_contract_unsupported",
            "provider_metadata_sensitive",
        }
        normalized = code if code in known_codes else "provider_entry_point_load_failed"
        return ConnectionProviderLoadDiagnostic(
            code=normalized,
            entry_point=entry_point,
            message="connection provider entry point was rejected",
        )

    @staticmethod
    def _installed() -> list[Any]:
        try:
            return list(metadata.entry_points(group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP))
        except TypeError:
            discovered = metadata.entry_points()
            selected = getattr(discovered, "select", None)
            if callable(selected):
                return list(selected(group=CONNECTION_PROVIDER_ENTRY_POINT_GROUP))
            return [
                point
                for point in discovered
                if getattr(point, "group", None) == CONNECTION_PROVIDER_ENTRY_POINT_GROUP
            ]


class _ProviderLoadError(ValueError):
    pass


__all__ = [
    "CONNECTION_PROVIDER_ENTRY_POINT_GROUP",
    "ConnectionProviderLoadDiagnostic",
    "ConnectionProviderLoader",
    "ConnectionProviderLoadResult",
]
