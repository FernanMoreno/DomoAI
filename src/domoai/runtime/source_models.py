"""Typed normalization for source devices, entities and capability routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from domoai.domain.models import Capability, SourceRef

_CANONICAL_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def _key_values(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set)):
        raise ValueError(f"{field_name} must be a list of strings")
    values = tuple(str(item).strip() for item in value)
    if any(not item for item in values):
        raise ValueError(f"{field_name} cannot contain empty values")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


def _canonical_id(value: object) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate or _CANONICAL_ID.fullmatch(candidate) is None:
        raise ValueError("canonical_id must use the canonical lowercase identifier format")
    return candidate


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "device"


def legacy_local_canonical_id(entity: dict[str, Any]) -> str:
    """Return the pre-composition ID used by existing adapters."""

    area_id = str(entity.get("area_id") or "unassigned")
    return f"{area_id}.{_slug(str(entity.get('name') or entity['entity_id']))}"


@dataclass(frozen=True)
class SourceIdentity:
    adapter_id: str
    source_device_id: str
    source_entity_id: str
    identity_keys: tuple[str, ...]
    connections: tuple[str, ...]
    parent_source_device_id: str | None
    explicit_canonical_id: str | None
    local_canonical_id: str

    @classmethod
    def from_entity(cls, entity: dict[str, Any], adapter_id: str) -> SourceIdentity:
        source_entity_id = str(entity.get("entity_id") or "").strip()
        if not source_entity_id:
            raise ValueError("source entity requires entity_id")
        source_device_id = str(
            entity.get("source_device_id") or entity.get("device_id") or source_entity_id
        ).strip()
        if not source_device_id:
            raise ValueError("source entity requires a source device identity")
        parent = entity.get("parent_source_device_id", entity.get("parent_device_id"))
        parent_id = str(parent).strip() if parent is not None else None
        if parent_id == "":
            raise ValueError("parent device identity cannot be empty")
        return cls(
            adapter_id=adapter_id,
            source_device_id=source_device_id,
            source_entity_id=source_entity_id,
            identity_keys=_key_values(entity.get("identity_keys"), "identity_keys"),
            connections=_key_values(entity.get("connections"), "connections"),
            parent_source_device_id=parent_id,
            explicit_canonical_id=_canonical_id(entity.get("canonical_id")),
            local_canonical_id=str(
                entity.get("local_canonical_id") or legacy_local_canonical_id(entity)
            ),
        )

    @property
    def source_key(self) -> str:
        return f"source:{self.adapter_id}:{self.source_device_id}"

    @property
    def identity_key(self) -> str:
        if self.explicit_canonical_id is not None:
            return f"canonical:{self.explicit_canonical_id}"
        if self.identity_keys:
            return f"source-identity:{self.adapter_id}:{'|'.join(sorted(self.identity_keys))}"
        if self.connections:
            return f"source-connection:{self.adapter_id}:{'|'.join(sorted(self.connections))}"
        return self.source_key


@dataclass(frozen=True)
class CapabilityRoute:
    canonical_device_id: str
    capability: str
    source_ref: SourceRef
    source_device_id: str
    local_canonical_id: str
    commands: tuple[str, ...]
    available: bool


@dataclass(frozen=True)
class RouteResolution:
    route: CapabilityRoute | None
    reason: str | None = None
    candidates: tuple[CapabilityRoute, ...] = ()


def capabilities_from_entity(entity: dict[str, Any]) -> list[Capability]:
    raw_capabilities = entity.get("capabilities", [])
    if not isinstance(raw_capabilities, list):
        raise ValueError("source entity capabilities must be a list")
    return [Capability.model_validate(item) for item in raw_capabilities]
