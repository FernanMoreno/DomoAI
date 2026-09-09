"""Deterministic, confirmation-aware connection repair orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC
from typing import Any
from uuid import UUID

from domoai.domain.connections import ConnectionDiagnostic
from domoai.domain.repairs import RepairAction, RepairActionName, RepairPlan, RepairRisk
from domoai.runtime.clock import Clock, SystemClock


class RepairEngineError(RuntimeError):
    """Sanitized repair planning or execution error."""


_CATALOG: dict[str, tuple[tuple[RepairActionName, RepairRisk], ...]] = {
    "authentication_required": ((RepairActionName.REAUTHENTICATE, RepairRisk.SECURITY),),
    "authentication_failed": ((RepairActionName.REAUTHENTICATE, RepairRisk.SECURITY),),
    "credential_required": ((RepairActionName.REAUTHENTICATE, RepairRisk.SECURITY),),
    "timeout": ((RepairActionName.RETRY, RepairRisk.SAFE),),
    "unreachable": ((RepairActionName.RETRY, RepairRisk.SAFE),),
    "knx_no_tunnel_slot": ((RepairActionName.RETRY, RepairRisk.SAFE),),
    "invalid_endpoint": ((RepairActionName.REPLACE_ENDPOINT, RepairRisk.CONFIGURATION),),
    "mapping_required": ((RepairActionName.IMPORT_MAPPING, RepairRisk.CONFIGURATION),),
    "mapping_invalid": ((RepairActionName.IMPORT_MAPPING, RepairRisk.CONFIGURATION),),
    "incompatible": ((RepairActionName.SELECT_PROFILE, RepairRisk.CONFIGURATION),),
    "tunnelling_unsupported": ((RepairActionName.SELECT_INTERFACE, RepairRisk.CONFIGURATION),),
    "interface_unknown": ((RepairActionName.SELECT_INTERFACE, RepairRisk.CONFIGURATION),),
    "knx_secure_interface_invalid": (
        (RepairActionName.SELECT_INTERFACE, RepairRisk.CONFIGURATION),
    ),
    "tls_certificate_invalid": ((RepairActionName.TRUST_CERTIFICATE, RepairRisk.SECURITY),),
    "knx_secure_credentials_required": (
        (RepairActionName.UPDATE_CREDENTIALS, RepairRisk.SECURITY),
    ),
    "knx_secure_configuration_invalid": (
        (RepairActionName.UPDATE_CREDENTIALS, RepairRisk.SECURITY),
    ),
}

_SENSITIVE = {
    "token",
    "password",
    "passwd",
    "secret",
    "bearer",
    "authorization",
    "api_key",
    "private_key",
    "credential",
}


class RepairEngine:
    """Build immutable repair plans and serialize execution per connection."""

    def __init__(self, manager: Any, *, clock: Clock | None = None) -> None:
        self.manager = manager
        self.clock = clock or SystemClock()
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def plan(self, connection_id: UUID) -> RepairPlan:
        diagnostics = tuple(await self.manager.diagnose(connection_id))
        diagnostics_digest = _digest_diagnostics(diagnostics)
        actions: list[RepairAction] = []
        seen: set[tuple[RepairActionName, str]] = set()
        for diagnostic_index, diagnostic in enumerate(diagnostics):
            candidates = _CATALOG.get(diagnostic.code, ())
            for action_index, (action_name, risk) in enumerate(candidates):
                key = (action_name, diagnostic.code)
                if key in seen:
                    continue
                seen.add(key)
                action_id = (
                    f"{action_name.value}-{diagnostic.code}-{diagnostic_index}-{action_index}"
                )
                actions.append(
                    RepairAction(
                        action_id=action_id,
                        action=action_name,
                        requires_confirmation=action_name is not RepairActionName.RETRY,
                        risk=risk,
                        diagnostic_code=diagnostic.code,
                        plan_digest=_digest_action(
                            diagnostics_digest, action_name, diagnostic.code
                        ),
                    )
                )
        return RepairPlan(
            connection_id=str(connection_id),
            diagnostics_digest=diagnostics_digest,
            actions=tuple(actions),
            created_at=self.clock.now().astimezone(UTC),
        )

    async def suggest(self, connection_id: UUID) -> RepairPlan:
        return await self.plan(connection_id)

    async def execute(
        self,
        plan: RepairPlan,
        action_id: str,
        *,
        confirmed: bool = False,
        parameters: Mapping[str, object] | None = None,
        credential: str | None = None,
    ) -> Any:
        connection_id = _connection_uuid(plan.connection_id)
        async with self._lock_for(connection_id):
            current_diagnostics = tuple(await self.manager.diagnose(connection_id))
            current_digest = _digest_diagnostics(current_diagnostics)
            if current_digest != plan.diagnostics_digest:
                raise RepairEngineError("repair plan is stale")
            action = next((item for item in plan.actions if item.action_id == action_id), None)
            if action is None:
                raise RepairEngineError("repair action is not in the plan")
            expected_digest = _digest_action(
                plan.diagnostics_digest, action.action, action.diagnostic_code
            )
            if expected_digest != action.plan_digest:
                raise RepairEngineError("repair action is invalid")
            safe_parameters = _safe_parameters(parameters)
            if action.action is RepairActionName.RETRY:
                if safe_parameters:
                    raise RepairEngineError("retry does not accept parameters")
                return await self.manager.repair(connection_id)
            if not confirmed:
                raise RepairEngineError("repair action requires confirmation")
            if action.action in {
                RepairActionName.REAUTHENTICATE,
                RepairActionName.UPDATE_CREDENTIALS,
            }:
                if not isinstance(credential, str) or not credential:
                    raise RepairEngineError("repair action requires a credential")
                return await self.manager.reauth(connection_id, credential)
            if credential is not None:
                raise RepairEngineError("credential is not accepted for this repair")
            return await self._execute_reconfiguration(
                connection_id, action.action, safe_parameters
            )

    async def _execute_reconfiguration(
        self,
        connection_id: UUID,
        action: RepairActionName,
        parameters: dict[str, object],
    ) -> Any:
        profile = await self.manager.get(connection_id)
        endpoint = dict(getattr(profile, "endpoint", {}))
        options = dict(getattr(profile, "options", {}))
        mapping_ref = getattr(profile, "mapping_ref", None)
        if action is RepairActionName.REPLACE_ENDPOINT:
            if "url" in parameters:
                endpoint["url"] = parameters["url"]
            if "host" in parameters:
                endpoint["host"] = parameters["host"]
            if "port" in parameters:
                endpoint["port"] = parameters["port"]
            if not {"url", "host"}.intersection(parameters):
                raise RepairEngineError("replace_endpoint requires a new endpoint")
        elif action is RepairActionName.SELECT_INTERFACE:
            interface = parameters.get("interface")
            if not isinstance(interface, str) or not interface:
                raise RepairEngineError("select_interface requires an interface")
            options["secure_interface"] = interface
        elif action is RepairActionName.IMPORT_MAPPING:
            value = parameters.get("mapping_ref")
            if not isinstance(value, str) or not value:
                raise RepairEngineError("import_mapping requires a mapping reference")
            mapping_ref = value
        elif action is RepairActionName.SELECT_PROFILE:
            value = parameters.get("profile")
            if not isinstance(value, str) or not value:
                raise RepairEngineError("select_profile requires a profile")
            options["profile"] = value
        elif action is RepairActionName.TRUST_CERTIFICATE:
            value = parameters.get("certificate_fingerprint")
            if not isinstance(value, str) or not value:
                raise RepairEngineError("trust_certificate requires a fingerprint")
            options["tls_trust_fingerprint"] = value
        else:
            raise RepairEngineError("repair action is not executable")
        return await self.manager.reconfigure(
            connection_id,
            endpoint=endpoint,
            options=options,
            mapping_ref=mapping_ref,
        )

    def _lock_for(self, connection_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(connection_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[connection_id] = lock
        return lock


def _digest_diagnostics(diagnostics: tuple[ConnectionDiagnostic, ...]) -> str:
    payload = [diagnostic.model_dump(mode="json") for diagnostic in diagnostics]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_action(
    diagnostics_digest: str, action: RepairActionName, diagnostic_code: str
) -> str:
    value = f"{diagnostics_digest}:{action.value}:{diagnostic_code}".encode()
    return hashlib.sha256(value).hexdigest()


def _safe_parameters(parameters: Mapping[str, object] | None) -> dict[str, object]:
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise RepairEngineError("repair parameters must be an object")
    safe: dict[str, object] = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or _contains_sensitive(key):
            raise RepairEngineError("repair parameters contain sensitive fields")
        if isinstance(value, bool | int | float | str):
            if isinstance(value, str) and _contains_sensitive(value):
                raise RepairEngineError("repair parameters contain sensitive material")
            safe[key] = value
        else:
            raise RepairEngineError("repair parameters must contain scalar values")
    return safe


def _contains_sensitive(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return any(term in normalized for term in _SENSITIVE)


def _connection_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        raise RepairEngineError("repair connection identity is invalid") from None


__all__ = ["RepairEngine", "RepairEngineError"]
