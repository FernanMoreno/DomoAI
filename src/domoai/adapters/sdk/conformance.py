"""Deterministic, local-first conformance checks for AdapterPort providers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from pydantic import Field

from domoai.domain.models import Command, Device, RiskClass, SourceRef, StrictModel
from domoai.runtime.registry import DeviceRegistry

from .manifest import (
    ADAPTER_CONTRACT_VERSION,
    AdapterManifest,
    CompatibilityDiagnostic,
    DiagnosticSeverity,
    sanitize_exception,
)
from .registry import AdapterRegistration


class ConformanceCheck(StrictModel):
    check_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    status: str = Field(pattern=r"^(passed|failed)$")
    message: str = Field(min_length=1, max_length=240)


class ConformanceResult(StrictModel):
    schema_version: str = "v1"
    adapter_id: str = Field(min_length=1)
    contract_version: str = ADAPTER_CONTRACT_VERSION
    fixture_profile: str = Field(min_length=1)
    status: str = Field(pattern=r"^(passed|failed)$")
    checks: list[ConformanceCheck] = Field(default_factory=list)
    diagnostics: list[CompatibilityDiagnostic] = Field(default_factory=list)


class ConformanceHarness:
    """Run safe AdapterPort checks against a deterministic fixture/provider."""

    def __init__(
        self,
        registration: AdapterRegistration,
        *,
        fixture_profile: str = "local-deterministic",
        event_probe_timeout_seconds: float = 0.05,
    ) -> None:
        self.registration = registration
        self.fixture_profile = fixture_profile
        self.event_probe_timeout_seconds = event_probe_timeout_seconds

    async def run(self) -> ConformanceResult:
        checks: list[ConformanceCheck] = []
        diagnostics: list[CompatibilityDiagnostic] = []
        adapter = None
        connected = False
        snapshot = None
        try:
            adapter = self.registration.create()
            _passed(checks, "factory", "factory returned a valid AdapterPort")
        except Exception as error:
            _failed(checks, "factory", "adapter factory failed", diagnostics, error)
            return _result(self.registration.manifest, self.fixture_profile, checks, diagnostics)

        try:
            try:
                await adapter.connect()
                connected = True
                _passed(checks, "connect", "adapter connected")
            except Exception as error:
                _failed(checks, "connect", "adapter connection failed", diagnostics, error)
                return _result(
                    self.registration.manifest, self.fixture_profile, checks, diagnostics
                )

            try:
                health = await adapter.health()
                if (
                    health.adapter_id != self.registration.manifest.adapter_id
                    or not health.connected
                ):
                    raise ValueError("health did not report the registered connected adapter")
                _passed(checks, "health", "health reports the registered adapter as connected")
            except Exception as error:
                _failed(checks, "health", "health contract failed", diagnostics, error)

            try:
                snapshot = await adapter.discover()
                _passed(checks, "discover", "discovery returned an AdapterSnapshot")
            except Exception as error:
                _failed(checks, "discover", "discovery failed", diagnostics, error)
                return _result(
                    self.registration.manifest, self.fixture_profile, checks, diagnostics
                )

            try:
                if await adapter.discover() != snapshot:
                    raise ValueError("repeated discovery produced different snapshots")
                _passed(checks, "stable_discovery", "repeated discovery is deterministic")
            except Exception as error:
                _failed(
                    checks,
                    "stable_discovery",
                    "discovery is not deterministic",
                    diagnostics,
                    error,
                )

            try:
                _validate_source_identity(snapshot.source_entities)
                _passed(checks, "source_identity", "source entity ids are present and unique")
            except Exception as error:
                _failed(
                    checks,
                    "source_identity",
                    "source identity contract failed",
                    diagnostics,
                    error,
                )

            try:
                _validate_availability(snapshot.source_entities, snapshot.source_states)
                _passed(checks, "availability", "availability is explicit for entities and states")
            except Exception as error:
                _failed(checks, "availability", "availability contract failed", diagnostics, error)

            try:
                await _validate_state_contract(adapter, snapshot.source_states)
                _passed(checks, "state_contract", "state reads preserve timestamps and SourceRef")
            except Exception as error:
                _failed(checks, "state_contract", "state contract failed", diagnostics, error)

            try:
                await _probe_events(adapter.subscribe_events(), self.event_probe_timeout_seconds)
                _passed(checks, "events", "event subscription can be probed safely")
            except Exception as error:
                _failed(checks, "events", "event subscription failed", diagnostics, error)

            try:
                await _check_safe_execution(adapter, snapshot)
                _passed(checks, "safe_execution", "safe command, readback and idempotency passed")
            except Exception as error:
                _failed(
                    checks,
                    "safe_execution",
                    "safe execution contract failed",
                    diagnostics,
                    error,
                )
        finally:
            if connected:
                try:
                    await adapter.disconnect()
                    _passed(checks, "disconnect", "adapter disconnected cleanly")
                except Exception as error:
                    _failed(checks, "disconnect", "adapter disconnect failed", diagnostics, error)

        return _result(self.registration.manifest, self.fixture_profile, checks, diagnostics)


def _result(
    manifest: AdapterManifest,
    fixture_profile: str,
    checks: list[ConformanceCheck],
    diagnostics: list[CompatibilityDiagnostic],
) -> ConformanceResult:
    ordered_checks = sorted(checks, key=lambda check: check.check_id)
    return ConformanceResult(
        adapter_id=manifest.adapter_id,
        fixture_profile=fixture_profile,
        status="passed" if all(check.status == "passed" for check in ordered_checks) else "failed",
        checks=ordered_checks,
        diagnostics=diagnostics,
    )


def _passed(checks: list[ConformanceCheck], check_id: str, message: str) -> None:
    checks.append(ConformanceCheck(check_id=check_id, status="passed", message=message))


def _failed(
    checks: list[ConformanceCheck],
    check_id: str,
    message: str,
    diagnostics: list[CompatibilityDiagnostic],
    error: BaseException,
) -> None:
    checks.append(ConformanceCheck(check_id=check_id, status="failed", message=message))
    diagnostics.append(
        CompatibilityDiagnostic(
            code="conformance_check_failed",
            severity=DiagnosticSeverity.ERROR,
            subject=check_id,
            message=f"{message} ({sanitize_exception(error)})",
        )
    )


def _validate_source_identity(entities: list[dict[str, Any]]) -> None:
    ids = [str(entity.get("entity_id", "")).strip() for entity in entities]
    if any(not entity_id for entity_id in ids):
        raise ValueError("source entity requires entity_id")
    if len(set(ids)) != len(ids):
        raise ValueError("source entity ids must be unique")


def _validate_availability(
    entities: list[dict[str, Any]], states: list[dict[str, Any]]
) -> None:
    if any(not isinstance(entity.get("available"), bool) for entity in entities):
        raise ValueError("source entity availability must be boolean")
    if any(not isinstance(state.get("available"), bool) for state in states):
        raise ValueError("source state availability must be boolean")


async def _validate_state_contract(adapter: Any, states: list[dict[str, Any]]) -> None:
    refs = [
        SourceRef(adapter_id=adapter.adapter_id, external_id=str(state["entity_id"]))
        for state in states[:1]
    ]
    if not refs:
        return
    snapshots = await adapter.read_state(refs)
    for snapshot in snapshots:
        if snapshot.source_ref.adapter_id != adapter.adapter_id:
            raise ValueError("state SourceRef adapter_id does not match provider")
        if snapshot.observed_at.tzinfo is None or snapshot.received_at.tzinfo is None:
            raise ValueError("state timestamps must be timezone-aware")
        if snapshot.received_at < snapshot.observed_at:
            raise ValueError("received_at must not precede observed_at")


async def _probe_events(stream: AsyncIterator[Any], timeout: float) -> None:
    iterator = stream.__aiter__()
    try:
        await asyncio.wait_for(anext(iterator), timeout=timeout)
    except StopAsyncIteration:
        return
    except TimeoutError:
        return
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()


async def _check_safe_execution(adapter: Any, snapshot: Any) -> None:
    registry = DeviceRegistry()
    devices, _areas = registry.apply_snapshot(snapshot, adapter.adapter_id)
    candidate = _safe_command_candidate(devices, registry)
    if candidate is None:
        return
    device, command_name = candidate
    route = registry.resolve_command_route(device.id, command_name).route
    if route is None:
        raise ValueError("safe command route was not resolvable")
    command = Command(
        id="sdk-conformance-command",
        device_id=device.id,
        command=command_name,
        value=_command_value(device, command_name),
        risk_class=RiskClass.SAFE,
        idempotency_key="sdk-conformance-idempotency",
    )
    first = await adapter.execute(command)
    if not first.accepted:
        raise ValueError("declared safe command was rejected")
    source_ref = first.source_ref or route.source_ref
    if not await adapter.read_state([source_ref]):
        raise ValueError("safe command did not provide readable postcondition")
    duplicate = await adapter.execute(
        Command.model_validate(
            {
                **command.model_dump(),
                "id": "sdk-conformance-duplicate",
            }
        )
    )
    if duplicate.accepted:
        raise ValueError("duplicate idempotency key was accepted")


def _safe_command_candidate(
    devices: list[Device], registry: DeviceRegistry
) -> tuple[Device, str] | None:
    safe_names = {
        "turn_on",
        "turn_off",
        "toggle",
        "set_brightness",
        "set_position",
        "open",
        "close",
        "stop",
        "set_temperature",
    }
    for device in devices:
        for capability in device.capabilities:
            for command in capability.commands:
                if capability.writable and command in safe_names:
                    if registry.resolve_command_route(device.id, command).route is not None:
                        return device, command
    return None


def _command_value(device: Device, command_name: str) -> int | float | None:
    if command_name in {"set_brightness", "set_position"}:
        capability_name = "brightness" if command_name == "set_brightness" else "position"
        capability = next(item for item in device.capabilities if item.name == capability_name)
        return capability.minimum if capability.minimum is not None else 0
    if command_name == "set_temperature":
        capability = next(
            item for item in device.capabilities if item.name == "target_temperature"
        )
        return capability.minimum if capability.minimum is not None else 20
    return None
