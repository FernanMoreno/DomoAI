"""Household intent with verified authority, current binding and a runtime clock."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Any, Literal, Never, cast

from domoai.application.authority import AuthorityPolicy
from domoai.domain.energy import DispatchableBatteryBinding
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.household_preferences import (
    Preference,
    PreferenceHistory,
    PreferenceHistoryRead,
    PreferenceModel,
    PreferenceMutation,
    PreferenceProfile,
    PreferenceRead,
    PreferenceRestore,
    PreferenceResult,
    PreferenceSlotMutation,
    PreferenceTarget,
    PreferenceVersion,
    PreferenceView,
)
from domoai.domain.models import Area, AuthorityContext, CapabilityKind, Device
from domoai.runtime.clock import Clock
from domoai.runtime.preference_ports import PreferenceRepositoryPort
from domoai.runtime.registry import DeviceRegistry


class HouseholdPreferenceService:
    def __init__(
        self,
        repository: PreferenceRepositoryPort,
        *,
        policy: AuthorityPolicy,
        registry: DeviceRegistry,
        clock: Clock,
        inventory_lock: asyncio.Lock,
        battery_binding: DispatchableBatteryBinding | None = None,
    ):
        if (repository.tenant_id, repository.household_id) != (
            policy.tenant_id,
            policy.household_id,
        ):
            raise ValueError("preference repository and authority scope must match")
        self.repository = repository
        self.policy = policy
        self.registry = registry
        self.clock = clock
        self.inventory_lock = inventory_lock
        self.battery_binding = battery_binding
        self._cursor_key = secrets.token_bytes(32)

    def authorize(self, authority: AuthorityContext, operation: str) -> None:
        self.policy.authorize(authority, operation=operation)

    async def read(self, request: PreferenceRead, authority: AuthorityContext) -> PreferenceProfile:
        self.authorize(authority, "get_household_preferences")
        if request.target is not None and request.target.alias is not None:
            resolved_target = self._resolve_inventory_target(request.target)
            request = request.model_copy(
                update={
                    "target": PreferenceTarget(kind=request.target.kind, id=resolved_target.id),
                }
            )
        version, slots = await self.repository.snapshot()
        now = self.clock.now()
        cursor = self._decode_cursor(request, authority, "profile", version)
        observed = datetime.fromisoformat(cursor["observed"]) if cursor else now
        if cursor and cursor["transition"] and now >= datetime.fromisoformat(cursor["transition"]):
            self._stale_cursor()
        views = []
        transitions = []
        for slot in slots:
            pref = slot.preference
            state: Literal["scheduled", "effective", "expired", "deleted"] = "deleted"
            if not slot.deleted and pref is not None:
                for instant in (pref.valid_from, pref.valid_until):
                    if instant is not None and instant > observed:
                        transitions.append(instant)
                state = (
                    "scheduled"
                    if pref.valid_from > observed
                    else "expired"
                    if pref.valid_until is not None and pref.valid_until <= observed
                    else "effective"
                )
            if request.key is not None and slot.key != request.key:
                continue
            if request.target is not None and slot.target != request.target:
                continue
            if not request.effective_only or state == "effective":
                views.append(PreferenceView(slot=slot, state=state))
        transition = min(transitions) if transitions else None
        offset = cursor["offset"] if cursor else 0
        selected = views[offset : offset + request.limit]
        next_cursor = None
        if offset + request.limit < len(views):
            next_cursor = self._encode_cursor(
                request,
                authority,
                "profile",
                version,
                offset=offset + request.limit,
                observed=observed.isoformat(),
                transition=transition.isoformat() if transition else None,
            )
        return PreferenceProfile(
            version=version,
            observed_at=observed,
            next_transition_at=transition,
            preferences=selected,
            next_cursor=next_cursor,
        )

    def _cursor_context(
        self,
        request: PreferenceRead | PreferenceHistoryRead,
        authority: AuthorityContext,
        kind: str,
        version: PreferenceVersion,
    ) -> dict[str, Any]:
        return {
            "tenant": self.policy.tenant_id,
            "household": self.policy.household_id,
            "principal": authority.principal_id,
            "kind": kind,
            "version": version.model_dump(mode="json"),
            "filter": request.model_dump(mode="json", exclude={"cursor"}),
        }

    def _encode_cursor(
        self,
        request: PreferenceRead | PreferenceHistoryRead,
        authority: AuthorityContext,
        kind: str,
        version: PreferenceVersion,
        **page: Any,
    ) -> str:
        data = json.dumps(
            self._cursor_context(request, authority, kind, version) | page,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return (
            base64.urlsafe_b64encode(data).decode()
            + "."
            + hmac.new(self._cursor_key, data, hashlib.sha256).hexdigest()
        )

    def _decode_cursor(
        self,
        request: PreferenceRead | PreferenceHistoryRead,
        authority: AuthorityContext,
        kind: str,
        version: PreferenceVersion,
    ) -> dict[str, Any] | None:
        if request.cursor is None:
            return None
        try:
            encoded, signature = request.cursor.split(".")
            data = base64.urlsafe_b64decode(encoded)
            if not hmac.compare_digest(
                signature, hmac.new(self._cursor_key, data, hashlib.sha256).hexdigest()
            ):
                self._stale_cursor()
            cursor = json.loads(data)
            expected = self._cursor_context(request, authority, kind, version)
            if any(cursor.get(key) != value for key, value in expected.items()):
                self._stale_cursor()
            return cast(dict[str, Any], cursor)
        except (ValueError, KeyError, TypeError):
            self._stale_cursor()

    @staticmethod
    def _stale_cursor() -> Never:
        raise DomainError(ErrorCode.STALE_CURSOR, "Restart preference pagination")

    async def history(
        self, request: PreferenceHistoryRead, authority: AuthorityContext
    ) -> PreferenceHistory:
        self.authorize(authority, "get_household_preference_history")
        version, _ = await self.repository.snapshot()
        cursor = self._decode_cursor(request, authority, "history", version)
        after = cursor["after"] if cursor else 0
        actual, entries = await self.repository.history(
            after_revision=after, limit=request.limit + 1, preference_id=request.preference_id
        )
        if actual != version:
            self._stale_cursor()
        selected = entries[: request.limit]
        next_cursor = (
            self._encode_cursor(
                request, authority, "history", version, after=selected[-1].version.revision
            )
            if len(entries) > request.limit
            else None
        )
        return PreferenceHistory(version=version, entries=selected, next_cursor=next_cursor)

    async def delete(
        self, request: PreferenceSlotMutation, authority: AuthorityContext
    ) -> PreferenceResult:
        self.authorize(authority, "delete_household_preference")
        return await self.repository.mutate(
            operation="delete",
            request=request,
            principal_id=authority.principal_id,
            now=self.clock.now(),
        )

    async def erase(
        self, request: PreferenceSlotMutation, authority: AuthorityContext
    ) -> PreferenceResult:
        self.authorize(authority, "erase_household_preference")
        return await self.repository.mutate(
            operation="erase",
            request=request,
            principal_id=authority.principal_id,
            now=self.clock.now(),
        )

    async def restore(
        self, request: PreferenceRestore, authority: AuthorityContext
    ) -> PreferenceResult:
        self.authorize(authority, "restore_household_preference")
        self.authorize(authority, "get_household_preference_history")
        receipt = await self.repository.replay(
            request=request,
            principal_id=authority.principal_id,
            fingerprint=self._fingerprint("restore", request),
        )
        if receipt is not None:
            return receipt
        async with self.inventory_lock:
            source = await self.repository.history_source(
                request.preference_id, request.source_revision
            )
            if source is None or source.snapshot is None or source.snapshot.preference is None:
                raise DomainError(ErrorCode.HISTORY_UNAVAILABLE, "History is unavailable")
            self.resolve(source.snapshot.preference)
            if request.valid_until is not None and request.valid_until <= self.clock.now():
                raise DomainError(ErrorCode.INVALID_PREFERENCE, "Preference has already expired")
            return await self.repository.mutate(
                operation="restore",
                request=request,
                principal_id=authority.principal_id,
                now=self.clock.now(),
            )

    def _resolve_inventory_target(self, target: PreferenceTarget) -> Device | Area:
        candidates = self.registry.devices if target.kind == "device" else self.registry.areas
        matches = [
            item
            for item in candidates
            if (
                item.id == target.id
                if target.id is not None
                else item.name.casefold() == (target.alias or "").casefold()
            )
        ]
        if len(matches) > 1:
            raise DomainError(
                ErrorCode.RESOLUTION_REQUIRED,
                "Choose an explicit inventory target",
                details={
                    "candidates": [
                        {"kind": target.kind, "id": item.id, "name": item.name}
                        for item in matches[:100]
                    ]
                },
            )
        if not matches:
            raise DomainError(ErrorCode.UNSUPPORTED_BINDING, "Preference target is unavailable")
        return matches[0]

    def resolve(self, pref: Preference) -> PreferenceTarget:
        target = pref.target
        selected = self._resolve_inventory_target(target)
        supported = False
        if pref.key == "comfort.temperature_target":
            devices = (
                [selected]
                if isinstance(selected, Device)
                else [device for device in self.registry.devices if device.area_id == selected.id]
            )
            supported = any(
                cap.unit == "degC" and cap.kind in {CapabilityKind.NUMBER, CapabilityKind.INTEGER}
                for device in devices
                for cap in device.capabilities
            )
        elif isinstance(selected, Device):
            if pref.key == "energy.minimum_soc":
                binding = self.battery_binding
                supported = (
                    binding is not None
                    and binding.device_id == selected.id
                    and binding.profile.capacity_kwh > 0
                    and any(cap.name == "battery.soc" for cap in selected.capabilities)
                )
            else:
                supported = any(cap.writable for cap in selected.capabilities)
        if not supported:
            raise DomainError(
                ErrorCode.UNSUPPORTED_BINDING, "Target does not support this preference"
            )
        return type(target)(kind=target.kind, id=selected.id)

    async def set(
        self, request: PreferenceMutation, authority: AuthorityContext
    ) -> PreferenceResult:
        self.authorize(authority, "set_household_preference")
        digest = self._fingerprint("set", request)
        receipt = await self.repository.replay(
            request=request, principal_id=authority.principal_id, fingerprint=digest
        )
        if receipt is not None:
            return receipt
        # Discovery and membership changes share this owner. Keep binding
        # validation through the awaited storage commit under its lock.
        async with self.inventory_lock:
            target = self.resolve(request.preference)
            now = self.clock.now()
            if request.preference.valid_until is not None and request.preference.valid_until <= now:
                raise DomainError(ErrorCode.INVALID_PREFERENCE, "Preference has already expired")
            bound = request.model_copy(
                update={"preference": request.preference.model_copy(update={"target": target})}
            )
            return await self.repository.mutate(
                operation="set",
                request=bound,
                principal_id=authority.principal_id,
                now=now,
                fingerprint=digest,
            )

    @staticmethod
    def _fingerprint(operation: str, request: PreferenceModel) -> str:
        return hashlib.sha256(
            (
                operation
                + ":"
                + json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            ).encode()
        ).hexdigest()
