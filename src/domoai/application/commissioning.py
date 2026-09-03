"""Read-only commissioning assessment over the canonical device registry."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC
from pathlib import Path

from domoai.domain.commissioning import (
    CommissioningAssetType,
    CommissioningBlocker,
    CommissioningCandidate,
    CommissioningCandidateStatus,
    CommissioningReport,
    CommissioningRoute,
)
from domoai.domain.models import Device, DeviceType
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.source_models import CapabilityRoute


class CommissioningPersistenceError(ValueError):
    """Raised when a report cannot be atomically persisted."""


_BATTERY_TELEMETRY = ("battery.soc", "battery.power", "battery.capacity")
_EV_TELEMETRY = ("ev.soc", "ev_charging", "ev.connected")


class CommissioningService:
    """Assess discovered assets without creating physical authority.

    The service intentionally accepts only a ``DeviceRegistry``.  Provider
    credentials, raw adapter clients and binding constructors do not cross this
    boundary, so discovery cannot silently become actuator authorization.
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        clock: Clock | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock or SystemClock()
        self.manifest_path = manifest_path

    def inspect(
        self,
        *,
        runtime_revision: str,
        asset_types: Sequence[CommissioningAssetType | str] | None = None,
        persist: bool = True,
    ) -> CommissioningReport:
        selected_types = self._asset_types(asset_types)
        candidates = [
            self._assess(device, asset_type)
            for device in self.registry.devices
            for asset_type in selected_types
            if self._matches(device, asset_type)
        ]
        candidates.sort(
            key=lambda candidate: (
                candidate.asset_type.value,
                candidate.canonical_device_id,
            )
        )
        report = self._build_report(runtime_revision, candidates)
        if persist and self.manifest_path is not None:
            self._persist(report)
        return report

    @staticmethod
    def _asset_types(
        asset_types: Sequence[CommissioningAssetType | str] | None,
    ) -> tuple[CommissioningAssetType, ...]:
        if asset_types is None:
            return tuple(CommissioningAssetType)
        selected = tuple(
            item if isinstance(item, CommissioningAssetType) else CommissioningAssetType(item)
            for item in asset_types
        )
        if len(set(selected)) != len(selected):
            raise ValueError("commissioning asset_types must be unique")
        return selected

    @staticmethod
    def _matches(device: Device, asset_type: CommissioningAssetType) -> bool:
        return (
            asset_type is CommissioningAssetType.BATTERY
            and device.type is DeviceType.ENERGY
        ) or (
            asset_type is CommissioningAssetType.EV_CHARGER
            and device.type is DeviceType.EV_CHARGER
        )

    def _assess(
        self,
        device: Device,
        asset_type: CommissioningAssetType,
    ) -> CommissioningCandidate:
        telemetry = (
            _BATTERY_TELEMETRY
            if asset_type is CommissioningAssetType.BATTERY
            else _EV_TELEMETRY
        )
        control_capability = (
            "battery_control"
            if asset_type is CommissioningAssetType.BATTERY
            else "ev_charging"
        )
        required = (*telemetry, control_capability)
        blockers: list[CommissioningBlocker] = []
        routes: list[CommissioningRoute] = []
        telemetry_ok = True

        for capability_name in required:
            capability = next(
                (item for item in device.capabilities if item.name == capability_name),
                None,
            )
            source_routes = self.registry.routes_for(device.id, capability_name)
            routes.extend(self._route(route) for route in source_routes)
            if capability is None:
                code = (
                    "missing_control_route"
                    if capability_name == control_capability
                    else "missing_capability"
                )
                blockers.append(
                    CommissioningBlocker(
                        code=code,
                        capability=capability_name,
                        detail=f"canonical capability {capability_name!r} was not discovered",
                    )
                )
                if capability_name != control_capability:
                    telemetry_ok = False
                continue

            available_routes = [route for route in source_routes if route.available]
            if len(available_routes) > 1:
                blockers.append(
                    CommissioningBlocker(
                        code="ambiguous_route",
                        capability=capability_name,
                        detail="more than one available source route requires explicit selection",
                    )
                )
                if capability_name != control_capability:
                    telemetry_ok = False
            elif not available_routes:
                blockers.append(
                    CommissioningBlocker(
                        code="source_unavailable",
                        capability=capability_name,
                        detail="no available source route is present",
                    )
                )
                if capability_name != control_capability:
                    telemetry_ok = False
            elif capability_name != control_capability and not available_routes[0].readable:
                blockers.append(
                    CommissioningBlocker(
                        code="unreadable_capability",
                        capability=capability_name,
                        detail="the available source route is not readable",
                    )
                )
                telemetry_ok = False

            if capability_name == control_capability:
                writable_routes = [route for route in available_routes if route.writable]
                if len(writable_routes) > 1:
                    blockers.append(
                        CommissioningBlocker(
                            code="ambiguous_route",
                            capability=capability_name,
                            detail=(
                                "more than one available writable route requires "
                                "explicit selection"
                            ),
                        )
                    )
                elif not writable_routes:
                    if available_routes:
                        blockers.append(
                            CommissioningBlocker(
                                code="missing_control_route",
                                capability=capability_name,
                                detail="the discovered route is not writable",
                            )
                        )
                    # ``source_unavailable`` was already emitted when there
                    # was no available route; do not duplicate that reason.

        if not device.identity_keys and not device.connections:
            blockers.append(
                CommissioningBlocker(
                    code="missing_stable_identity",
                    detail="no provider identity key or connection claim was discovered",
                )
            )

        blocker_codes = {blocker.code for blocker in blockers}
        control_blocked = bool(
            {"missing_control_route", "ambiguous_route", "source_unavailable"}
            & blocker_codes
        )
        if not telemetry_ok or "missing_stable_identity" in blocker_codes:
            status = CommissioningCandidateStatus.BLOCKED
        elif control_blocked:
            status = CommissioningCandidateStatus.OBSERVED_ONLY
        else:
            status = CommissioningCandidateStatus.READY_FOR_BINDING

        provider_ids = sorted(
            {
                route.provider_id
                for route in routes
            }
            | {source_ref.adapter_id for source_ref in device.source_refs}
        )
        next_actions = self._next_actions(asset_type, status)
        candidate_facts = {
            "asset_type": asset_type.value,
            "canonical_device_id": device.id,
            "provider_ids": provider_ids,
            "source_refs": [ref.model_dump(mode="json") for ref in device.source_refs],
            "identity_keys": sorted(device.identity_keys),
            "connections": sorted(device.connections),
            "required_capabilities": list(required),
            "routes": [route.model_dump(mode="json") for route in routes],
            "status": status.value,
            "blockers": [blocker.model_dump(mode="json") for blocker in blockers],
        }
        digest = _digest(candidate_facts)
        return CommissioningCandidate(
            asset_type=asset_type,
            canonical_device_id=device.id,
            name=device.name,
            device_type=device.type,
            provider_ids=provider_ids,
            source_refs=device.source_refs,
            identity_keys=sorted(device.identity_keys),
            connections=sorted(device.connections),
            required_capabilities=list(required),
            routes=routes,
            status=status,
            blockers=blockers,
            next_actions=next_actions,
            candidate_digest=digest,
        )

    @staticmethod
    def _route(route: CapabilityRoute) -> CommissioningRoute:
        return CommissioningRoute(
            provider_id=route.source_ref.adapter_id,
            capability=route.capability,
            source_ref=route.source_ref,
            source_device_id=route.source_device_id,
            commands=list(route.commands),
            readable=route.readable,
            writable=route.writable,
            available=route.available,
        )

    @staticmethod
    def _next_actions(
        asset_type: CommissioningAssetType,
        status: CommissioningCandidateStatus,
    ) -> list[str]:
        if status is CommissioningCandidateStatus.READY_FOR_BINDING:
            return ["provide_server_owned_binding"]
        if status is CommissioningCandidateStatus.OBSERVED_ONLY:
            return ["provide_authorized_control_route", "provide_server_owned_binding"]
        action = (
            "complete_battery_routes_and_identity"
            if asset_type is CommissioningAssetType.BATTERY
            else "complete_ev_routes_and_identity"
        )
        return [action]

    def _build_report(
        self,
        runtime_revision: str,
        candidates: list[CommissioningCandidate],
    ) -> CommissioningReport:
        generated_at = self.clock.now().astimezone(UTC)
        report_facts = {
            "schema_version": "v1",
            "runtime_revision": runtime_revision,
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            "warnings": [],
            "authority_created": False,
        }
        return CommissioningReport(
            runtime_revision=runtime_revision,
            generated_at=generated_at,
            report_digest=_digest(report_facts),
            candidates=candidates,
            warnings=[],
        )

    def _persist(self, report: CommissioningReport) -> None:
        assert self.manifest_path is not None
        path = self.manifest_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(
                        report.model_dump(mode="json"),
                        stream,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, path)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
        except (OSError, TypeError, ValueError) as error:
            raise CommissioningPersistenceError(
                "commissioning report could not be persisted atomically"
            ) from error


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["CommissioningPersistenceError", "CommissioningService"]
