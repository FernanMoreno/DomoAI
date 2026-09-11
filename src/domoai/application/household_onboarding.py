"""Bounded read projections over the existing household runtime."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Protocol, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from domoai.application.authority import AuthorityPolicy
from domoai.application.connection_manager import ConnectionManager
from domoai.application.state_service import StateService
from domoai.domain.coverage import CoverageEvidenceSnapshot
from domoai.domain.errors import DomainError
from domoai.domain.household_onboarding import (
    HouseholdOnboardingInventory,
    HouseholdOnboardingRead,
    HouseholdOnboardingStatus,
    OnboardingAuthMode,
    OnboardingCheck,
    OnboardingClientConnection,
    OnboardingRecoveryCode,
)
from domoai.domain.models import AuthorityContext, Device
from domoai.runtime.identity import runtime_adapter_id
from domoai.runtime.registry import DeviceRegistry


class CoverageProjector(Protocol):
    def __call__(
        self,
        registry: DeviceRegistry,
        *,
        runtime_revision: str,
        active_provider_ids: tuple[str, ...],
        devices: list[Device],
    ) -> CoverageEvidenceSnapshot: ...


class OnboardingProjectionError(Exception):
    def __init__(self, code: str, status: int) -> None:
        self.code, self.status = code, status
        super().__init__(code)


class HouseholdOnboardingService:
    def __init__(
        self,
        *,
        registry: DeviceRegistry,
        state_service: StateService,
        manager: ConnectionManager,
        authority_policy: AuthorityPolicy,
        coverage_projector: CoverageProjector,
        membership: Callable[[], tuple[str, ...]],
        readiness: Callable[[], list[OnboardingCheck]],
        public_url: str,
    ) -> None:
        self.registry, self.state_service, self.manager = registry, state_service, manager
        self.authority_policy, self.coverage_projector = authority_policy, coverage_projector
        self.membership, self.readiness = membership, readiness
        try:
            url = urlsplit(public_url)
            self.public_url = (
                urlunsplit((url.scheme, url.netloc, url.path, "", ""))
                if url.scheme in {"http", "https"}
                and url.hostname
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                else None
            )
        except ValueError:
            self.public_url = None

    def _authorize(
        self,
        authority: AuthorityContext,
        *,
        area_id: str | None = None,
        device_ids: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = (),
    ) -> None:
        try:
            self.authority_policy.authorize(
                authority,
                operation="list",
                area_id=area_id,
                device_ids=device_ids,
                capabilities=capabilities,
            )
        except DomainError:
            raise OnboardingProjectionError("forbidden", 403) from None

    async def status(
        self,
        authority: AuthorityContext,
        *,
        auth_mode: OnboardingAuthMode,
        manage_scope: bool,
    ) -> HouseholdOnboardingStatus:
        self._authorize(authority)
        can_manage = manage_scope
        try:
            self.authority_policy.authorize(authority, operation="manage_connections")
        except DomainError:
            can_manage = False
        recovery = {item.code for item in await self.manager.historical_recovery_diagnostics()}
        return HouseholdOnboardingStatus(
            auth_mode=auth_mode,
            can_manage_connections=can_manage,
            checks=self.readiness(),
            recovery_diagnostics=[
                cast(OnboardingRecoveryCode, code)
                for code in ("connection_recovery_pending", "connection_persistence_pending")
                if code in recovery
            ],
            client_connection=OnboardingClientConnection(
                url=self.public_url,
                auth_mode=auth_mode,
                status="prepared" if self.public_url else "pending",
            ),
        )

    def _visible(self, authority: AuthorityContext) -> list[Device]:
        return [
            device.model_copy(
                deep=True,
                update={
                    "capabilities": [
                        cap.model_copy(deep=True)
                        for cap in device.capabilities
                        if not authority.capabilities or cap.name in authority.capabilities
                    ]
                },
            )
            for device in sorted(self.registry.devices, key=lambda item: item.id)
            if (not authority.device_ids or device.id in authority.device_ids)
            and (not authority.area_ids or device.area_id in authority.area_ids)
        ]

    def inventory(
        self,
        authority: AuthorityContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> HouseholdOnboardingInventory:
        self._authorize(authority)
        if not 1 <= limit <= 100:
            raise ValueError("invalid limit")
        devices = self._visible(authority)
        store = self.state_service.state_store
        digest = hashlib.sha256(
            json.dumps(
                [
                    authority.model_dump(mode="json"),
                    store.runtime_revision,
                    [device.model_dump(mode="json") for device in devices],
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        offset = 0
        if cursor is not None:
            try:
                if len(cursor) > 256:
                    raise ValueError("invalid cursor")
                previous, offset = json.loads(
                    base64.b64decode(cursor, altchars=b"-_", validate=True)
                )
                if previous != digest or type(offset) is not int or not 0 <= offset <= len(devices):
                    raise ValueError("invalid cursor")
            except (ValueError, TypeError):
                raise ValueError("invalid cursor") from None
        page = devices[offset : offset + limit]
        next_cursor = (
            base64.urlsafe_b64encode(json.dumps([digest, offset + limit]).encode()).decode()
            if offset + limit < len(devices)
            else None
        )
        try:
            membership = tuple(self.membership())
        except Exception:
            raise OnboardingProjectionError("source_unavailable", 503) from None
        evidence = self.coverage_projector(
            self.registry,
            runtime_revision=store.runtime_revision,
            active_provider_ids=membership,
            devices=page,
        )
        return HouseholdOnboardingInventory(
            runtime_revision=store.runtime_revision,
            observed_at=store.clock.now(),
            devices=page,
            next_cursor=next_cursor,
            coverage_evidence=evidence,
        )

    async def read(
        self,
        authority: AuthorityContext,
        device_id: str,
        *,
        capability: str | None = None,
        connection_id: UUID | None = None,
    ) -> HouseholdOnboardingRead:
        self._authorize(
            authority, device_ids=(device_id,), capabilities=(capability,) if capability else ()
        )
        device = self.registry.get(device_id)
        if device is None:
            raise OnboardingProjectionError("not_found", 404)
        if authority.area_ids and device.area_id not in authority.area_ids:
            raise OnboardingProjectionError("forbidden", 403)
        self._authorize(authority, area_id=device.area_id)
        if connection_id is not None:
            await self.manager.get(connection_id)
        selected = [
            cap.name
            for cap in device.capabilities
            if cap.readable
            and (capability is None or cap.name == capability)
            and (not authority.capabilities or cap.name in authority.capabilities)
        ]
        store = self.state_service.state_store
        result = HouseholdOnboardingRead(
            runtime_revision=store.runtime_revision,
            observed_at=store.clock.now(),
            device_id=device_id,
        )
        if not selected:
            result.diagnostics = ["capability_unavailable"]
            return result
        reading = await self.state_service.get_with_diagnostics(
            [device_id],
            selected,
            allow_stale=False,
        )
        try:
            membership = frozenset(self.membership())
        except Exception:
            raise OnboardingProjectionError("source_unavailable", 503) from None
        for state in reading.states:
            source = state.source_ref
            if connection_id is not None and source.adapter_id != runtime_adapter_id(connection_id):
                result.diagnostics.append("connection_mismatch")
            elif source.adapter_id not in membership:
                result.diagnostics.append("source_unavailable")
            elif not any(
                route.available
                and route.readable
                and (route.source_ref.adapter_id, route.source_ref.external_id)
                == (source.adapter_id, source.external_id)
                for route in self.registry.routes_for(device_id, state.capability)
            ):
                result.diagnostics.append("source_mismatch")
            else:
                result.states.append(state)
        for reason in dict.fromkeys(item.reason for item in reading.diagnostics):
            if reason == "stale":
                result.diagnostics.append("stale")
            elif reason == "invalid":
                result.diagnostics.append("invalid")
            else:
                result.diagnostics.append("unavailable")
        if not reading.states and not reading.diagnostics:
            result.diagnostics = ["state_missing"]
        result.diagnostics = list(dict.fromkeys(result.diagnostics))
        result.read_status = "verified" if result.states else "pending"
        return result
