"""Canonical device registry, source identity and capability routing."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from domoai.domain.models import (
    AdapterSnapshot,
    Area,
    AvailabilityStatus,
    Capability,
    Device,
    DeviceType,
    SourceRef,
)
from domoai.runtime.source_models import (
    CapabilityRoute,
    RouteResolution,
    SourceIdentity,
    capabilities_from_entity,
)


class DeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._areas: dict[str, Area] = {}
        self._source_device_ids: dict[tuple[str, str], str] = {}
        self._source_entity_ids: dict[tuple[str, str], str] = {}
        self._identity_to_canonical: dict[str, str] = {}
        self._routes: dict[tuple[str, str], list[CapabilityRoute]] = {}
        self._parent_source_devices: dict[tuple[str, str], str] = {}
        self._diagnostics: list[dict[str, Any]] = []

    def load_persisted(self, devices: list[Device]) -> None:
        """Restore a readable-but-not-yet-executable inventory from persistence.

        Only ``_devices`` and ``_source_entity_ids`` are populated; routes,
        identity mappings and source-device mappings are rebuilt only by a
        live ``apply_snapshot`` this session, so a restored device cannot be
        resolved for execution until the runtime has reconfirmed it.
        """

        for device in devices:
            self._devices[device.id] = device
            for source_ref in device.source_refs:
                self._source_entity_ids[(source_ref.adapter_id, source_ref.external_id)] = device.id

    def apply_snapshot(
        self, snapshot: AdapterSnapshot, adapter_id: str
    ) -> tuple[list[Device], list[Area]]:
        for raw_area in snapshot.areas:
            area_id = str(raw_area["id"])
            self._areas[area_id] = Area(
                id=area_id,
                name=str(raw_area.get("name") or area_id.replace("_", " ").title()),
            )

        seen: dict[str, set[str]] = defaultdict(set)
        for entity in snapshot.source_entities:
            effective_adapter_id = str(
                entity.get("source_adapter_id")
                or entity.get("_adapter_id")
                or entity.get("adapter_id")
                or adapter_id
            )
            seen.setdefault(effective_adapter_id, set())
            try:
                source_entity_id = self._upsert_entity(entity, effective_adapter_id)
            except (TypeError, ValueError) as error:
                self._diagnostics.append(
                    {
                        "kind": "source_entity_rejected",
                        "adapter_id": effective_adapter_id,
                        "entity_id": str(entity.get("entity_id", "")),
                        "reason": str(error)[:200],
                    }
                )
            else:
                seen[effective_adapter_id].add(source_entity_id)

        self._reconcile(seen, snapshot.unsupported_sources, adapter_id)

        return self.devices, self.areas

    @property
    def devices(self) -> list[Device]:
        return [self._devices[key] for key in sorted(self._devices)]

    @property
    def areas(self) -> list[Area]:
        return [self._areas[key] for key in sorted(self._areas)]

    @property
    def diagnostics(self) -> list[dict[str, Any]]:
        return list(self._diagnostics)

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        """Return and clear diagnostics accumulated since the last drain."""

        diagnostics = self._diagnostics
        self._diagnostics = []
        return diagnostics

    def canonical_id_for_source(self, adapter_id: str, external_entity_id: str) -> str | None:
        return self._source_entity_ids.get((adapter_id, external_entity_id))

    def parent_source_device_for(self, adapter_id: str, source_device_id: str) -> str | None:
        return self._parent_source_devices.get((adapter_id, source_device_id))

    def mark_source_unavailable(self, adapter_id: str) -> None:
        for key, routes in self._routes.items():
            updated = [
                route
                if route.source_ref.adapter_id != adapter_id
                else CapabilityRoute(
                    canonical_device_id=route.canonical_device_id,
                    capability=route.capability,
                    source_ref=route.source_ref,
                    source_device_id=route.source_device_id,
                    local_canonical_id=route.local_canonical_id,
                    commands=route.commands,
                    available=False,
                )
                for route in routes
            ]
            self._routes[key] = updated
            self._refresh_device_availability(key[0])

    def _reconcile(
        self,
        seen: dict[str, set[str]],
        unsupported_sources: list[dict[str, Any]],
        default_adapter_id: str,
    ) -> None:
        failed_adapter_ids = {
            str(diagnostic.get("adapter_id", default_adapter_id))
            for diagnostic in unsupported_sources
            if diagnostic.get("failure")
        }
        authoritative = set(seen) - failed_adapter_ids
        if not authoritative:
            return
        for canonical_id in list(self._devices):
            device = self._devices.get(canonical_id)
            if device is None:
                continue
            stale_refs = [
                ref
                for ref in device.source_refs
                if ref.adapter_id in authoritative and ref.external_id not in seen[ref.adapter_id]
            ]
            if stale_refs:
                self._remove_source_refs(canonical_id, stale_refs)

    def _remove_source_refs(self, canonical_id: str, stale_refs: list[SourceRef]) -> None:
        stale_keys = {(ref.adapter_id, ref.external_id) for ref in stale_refs}
        for (device_id, capability), routes in list(self._routes.items()):
            if device_id != canonical_id:
                continue
            remaining = [
                route
                for route in routes
                if (route.source_ref.adapter_id, route.source_ref.external_id) not in stale_keys
            ]
            if remaining:
                self._routes[(device_id, capability)] = remaining
            else:
                del self._routes[(device_id, capability)]
        for adapter_id, external_id in stale_keys:
            self._source_entity_ids.pop((adapter_id, external_id), None)

        device = self._devices.get(canonical_id)
        if device is None:
            return
        remaining_refs = [
            ref for ref in device.source_refs if (ref.adapter_id, ref.external_id) not in stale_keys
        ]
        if remaining_refs:
            self._devices[canonical_id] = device.model_copy(update={"source_refs": remaining_refs})
            self._refresh_device_availability(canonical_id)
            return

        del self._devices[canonical_id]
        for key, value in list(self._source_device_ids.items()):
            if value == canonical_id:
                del self._source_device_ids[key]

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def routes_for(self, device_id: str, capability: str) -> tuple[CapabilityRoute, ...]:
        return tuple(self._routes.get((device_id, capability), ()))

    def resolve_command_route(self, device_id: str, command_name: str) -> RouteResolution:
        device = self._devices.get(device_id)
        if device is None:
            return RouteResolution(route=None, reason="device_not_found")
        capability = next(
            (
                item
                for item in device.capabilities
                if item.writable and command_name in item.commands
            ),
            None,
        )
        if capability is None:
            return RouteResolution(route=None, reason="unsupported_command")
        candidates = self.routes_for(device_id, capability.name)
        if len(candidates) != 1:
            return RouteResolution(
                route=None,
                reason="ambiguous_route" if candidates else "route_not_found",
                candidates=candidates,
            )
        route = candidates[0]
        if not route.available:
            return RouteResolution(
                route=None,
                reason="source_unavailable",
                candidates=candidates,
            )
        return RouteResolution(route=route, candidates=candidates)

    def _upsert_entity(self, entity: dict[str, Any], adapter_id: str) -> str:
        identity = SourceIdentity.from_entity(entity, adapter_id)
        canonical_id = self._canonical_id_for(identity)
        capabilities = capabilities_from_entity(entity)
        available = bool(entity.get("available", True))
        source_ref = SourceRef(
            adapter_id=adapter_id,
            external_id=identity.source_entity_id,
            external_type=str(entity.get("domain") or "unknown"),
        )
        existing = self._devices.get(canonical_id)
        merged_capabilities = self._merge_capabilities(existing, capabilities, canonical_id)
        source_refs = self._merge_source_refs(existing, source_ref)
        source_protocols = {ref.adapter_id for ref in source_refs}
        semantic_type = DeviceType(str(entity.get("semantic_type", "unsupported")))
        if existing is not None and existing.type is not semantic_type:
            self._diagnostics.append(
                {
                    "kind": "canonical_type_conflict",
                    "device_id": canonical_id,
                    "reason": "source contributions disagree on semantic type",
                }
            )
            semantic_type = existing.type
        device = Device(
            id=canonical_id,
            type=semantic_type,
            name=(
                existing.name if existing is not None else str(entity.get("name") or canonical_id)
            ),
            area_id=(
                existing.area_id
                if existing is not None and existing.area_id is not None
                else str(entity.get("area_id"))
                if entity.get("area_id")
                else None
            ),
            manufacturer=(
                existing.manufacturer
                if existing is not None and existing.manufacturer is not None
                else entity.get("manufacturer")
            ),
            model=(
                existing.model
                if existing is not None and existing.model is not None
                else entity.get("model")
            ),
            protocol=(next(iter(source_protocols)) if len(source_protocols) == 1 else "composite"),
            capabilities=merged_capabilities,
            availability=(
                AvailabilityStatus.AVAILABLE
                if available
                or (existing is not None and existing.availability is AvailabilityStatus.AVAILABLE)
                else AvailabilityStatus.UNAVAILABLE
            ),
            source_refs=source_refs,
        )
        self._devices[canonical_id] = device
        self._source_device_ids[(adapter_id, identity.source_device_id)] = canonical_id
        self._source_entity_ids[(adapter_id, identity.source_entity_id)] = canonical_id
        if identity.parent_source_device_id is not None:
            self._parent_source_devices[(adapter_id, identity.source_device_id)] = (
                identity.parent_source_device_id
            )
        for capability in capabilities:
            route = CapabilityRoute(
                canonical_device_id=canonical_id,
                capability=capability.name,
                source_ref=source_ref,
                source_device_id=identity.source_device_id,
                local_canonical_id=identity.local_canonical_id,
                commands=tuple(capability.commands),
                available=available,
            )
            routes = self._routes.setdefault((canonical_id, capability.name), [])
            existing_route = next(
                (index for index, item in enumerate(routes) if item.source_ref == route.source_ref),
                None,
            )
            if existing_route is None:
                routes.append(route)
            else:
                routes[existing_route] = route
        self._refresh_device_availability(canonical_id)
        return identity.source_entity_id

    def _refresh_device_availability(self, canonical_id: str) -> None:
        device = self._devices.get(canonical_id)
        if device is None:
            return
        routes = [
            route
            for (device_id, _capability), values in self._routes.items()
            if device_id == canonical_id
            for route in values
        ]
        if not routes:
            return
        availability = (
            AvailabilityStatus.AVAILABLE
            if any(route.available for route in routes)
            else AvailabilityStatus.UNAVAILABLE
        )
        if device.availability is not availability:
            self._devices[canonical_id] = device.model_copy(update={"availability": availability})

    def _canonical_id_for(self, identity: SourceIdentity) -> str:
        existing = self._identity_to_canonical.get(identity.identity_key)
        if existing is not None:
            return existing
        canonical_id = identity.explicit_canonical_id or identity.local_canonical_id
        if identity.explicit_canonical_id is None:
            claimed_ids = {
                candidate_id
                for key, candidate_id in self._identity_to_canonical.items()
                if key != identity.identity_key
            }
            if canonical_id in claimed_ids:
                suffix = 2
                base = canonical_id
                while f"{base}-{suffix}" in claimed_ids:
                    suffix += 1
                canonical_id = f"{base}-{suffix}"
        self._identity_to_canonical[identity.identity_key] = canonical_id
        return canonical_id

    def _merge_capabilities(
        self,
        existing: Device | None,
        capabilities: list[Capability],
        canonical_id: str,
    ) -> list[Capability]:
        merged = {
            capability.name: capability
            for capability in (existing.capabilities if existing else [])
        }
        for capability in capabilities:
            previous = merged.get(capability.name)
            if previous is not None and previous != capability:
                self._diagnostics.append(
                    {
                        "kind": "capability_metadata_conflict",
                        "device_id": canonical_id,
                        "capability": capability.name,
                        "reason": "source contributions disagree on capability metadata",
                    }
                )
                continue
            merged[capability.name] = capability
        return [merged[key] for key in sorted(merged)]

    @staticmethod
    def _merge_source_refs(existing: Device | None, source_ref: SourceRef) -> list[SourceRef]:
        refs = list(existing.source_refs if existing else [])
        if not any(
            ref.adapter_id == source_ref.adapter_id and ref.external_id == source_ref.external_id
            for ref in refs
        ):
            refs.append(source_ref)
        return refs
