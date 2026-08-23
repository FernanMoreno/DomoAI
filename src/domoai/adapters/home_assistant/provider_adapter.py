"""AdapterPort bridge for the Home Assistant Provider SDK implementation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import Literal, cast

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    ControlLeaseStatus,
    ExecutionStatus,
    PhysicalBaseline,
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
    StateStatus,
    TakeoverResult,
)
from domoai.domain.provider import ProviderCommand
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.control_takeover import ControlTakeoverRequest
from domoai.runtime.execution_context import ExecutionContext


class HomeAssistantProviderAdapter:
    """Expose one Provider SDK object through the established runtime boundary.

    The provider remains responsible for Home Assistant normalization and safe
    service translation. This bridge only projects snapshots and commands into
    the existing AdapterPort contract consumed by the registry, StateStore,
    executor and MCP services.
    """

    adapter_id = "home_assistant"

    def __init__(self, provider: HomeAssistantProvider, *, clock: Clock | None = None) -> None:
        self.provider = provider
        self._clock = clock or SystemClock()
        self._connected = False
        self._source_by_command: dict[tuple[str, str], list[str]] = {}
        self._canonical_by_source: dict[str, str] = {}

    async def connect(self) -> None:
        await self.provider.connect()
        self._connected = True

    async def disconnect(self) -> None:
        if self._connected:
            await self.provider.disconnect()
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        snapshot = await self.provider.snapshot()
        self._remember_sources(snapshot)
        return snapshot

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        wanted = {source_ref.external_id for source_ref in source_refs}
        snapshot = await self.provider.snapshot()
        now = self._clock.now()
        return [
            StateSnapshot(
                device_id=str(state["entity_id"]),
                capability=str(state["capability"]),
                value=state.get("value"),
                unit=state.get("unit"),
                observed_at=now,
                received_at=now,
                status=(
                    StateStatus.CURRENT if state.get("available", True) else StateStatus.UNAVAILABLE
                ),
                source_ref=SourceRef(
                    adapter_id=self.adapter_id,
                    external_id=str(state["entity_id"]),
                ),
            )
            for state in snapshot.source_states
            if str(state["entity_id"]) in wanted
        ]

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        candidates = self._source_by_command.get((command.device_id, command.command), [])
        if len(candidates) != 1:
            message = (
                "Ambiguous Home Assistant capability route"
                if len(candidates) > 1
                else f"Unknown Home Assistant command route: {command.command}"
            )
            return AdapterExecutionAck(accepted=False, message=message)
        return await self.execute_source(command, candidates[0], execution_context)

    async def execute_source(
        self,
        command: Command,
        source_entity_id: str,
        execution_context: ExecutionContext | None = None,
    ) -> AdapterExecutionAck:
        if source_entity_id not in self._canonical_by_source:
            return AdapterExecutionAck(
                accepted=False,
                message=f"Unknown Home Assistant entity: {source_entity_id}",
            )
        params = {} if command.value is None else {"value": command.value}
        provider_command = ProviderCommand(
            provider_id=self.provider.manifest.provider_id,
            external_device_id=source_entity_id,
            command=command.command,
            params=params,
            idempotency_key=command.idempotency_key,
            intent=command.intent,
        )
        if execution_context is None:
            result = await self.provider.execute(provider_command)
        else:
            result = await self.provider.execute(provider_command, execution_context)
        if result.status in {ExecutionStatus.FAILED, ExecutionStatus.UNAVAILABLE}:
            raise ConnectionError(result.message or "Home Assistant service call failed")
        if result.status not in {
            ExecutionStatus.ACCEPTED,
            ExecutionStatus.CONFIRMED_SUCCESS,
        }:
            return AdapterExecutionAck(
                accepted=False,
                source_ref=result.source_ref,
                message=result.message or "Home Assistant provider rejected command",
            )
        return AdapterExecutionAck(
            accepted=True,
            source_ref=result.source_ref
            or SourceRef(adapter_id=self.adapter_id, external_id=source_entity_id),
            message=result.message,
        )

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        """Read a declared HA battery baseline without pretending to disable native control.

        HA route mappings currently expose telemetry and service commands, but
        do not expose a certified native-scheduler disable operation. Active
        native ownership therefore fails closed here. The executor performs the
        first command and confirms its readback after this admission handshake.
        """

        now = self._clock.now()
        binding = next(
            (
                item
                for item in self.provider.battery_dispatch_bindings.values()
                if item.device_id == request.device_id
            ),
            None,
        )
        if binding is None:
            return self._takeover_rejected(request, now, "battery_binding_not_found")
        if request.native_scheduler_status == "active":
            return self._takeover_rejected(
                request, now, "native_scheduler_disable_not_configured"
            )
        snapshot = await self.provider.snapshot()
        state = next(
            (
                item
                for item in snapshot.source_states
                if str(item.get("entity_id")) == binding.power_feedback_entity_id
                and str(item.get("capability")) == binding.power_feedback_capability
            ),
            None,
        )
        value = state.get("value") if state is not None else None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return self._takeover_rejected(request, now, "baseline_unavailable")
        if state is None or not state.get("available", True):
            return self._takeover_rejected(request, now, "baseline_unavailable")
        baseline = PhysicalBaseline(
            device_id=request.device_id,
            capability=binding.power_feedback_capability,
            power_kw=float(value),
            observed_at=now,
            received_at=now,
            source_ref=SourceRef(
                adapter_id=self.adapter_id,
                external_id=binding.power_feedback_entity_id,
            ),
            state_revision=f"ha:{now.isoformat()}",
            native_scheduler_status=cast(
                Literal["disabled", "inactive", "active", "unknown"],
                request.native_scheduler_status,
            ),
        )
        return TakeoverResult(
            lease_id=f"ha-control-{request.device_id}-{request.plan_id}",
            status=ControlLeaseStatus.ACQUIRED,
            owner=request.owner,
            device_id=request.device_id,
            plan_id=request.plan_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=request.lease_seconds),
            baseline=baseline,
            first_command_id=request.first_command_id,
            first_command_confirmed=False,
            evidence_digest=self._takeover_digest(request, baseline),
        )

    def _takeover_rejected(
        self, request: ControlTakeoverRequest, now: datetime, failure_code: str
    ) -> TakeoverResult:
        return TakeoverResult(
            lease_id=f"rejected-{request.plan_id}",
            status=ControlLeaseStatus.REJECTED,
            owner=request.owner,
            device_id=request.device_id,
            plan_id=request.plan_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=request.lease_seconds),
            first_command_id=request.first_command_id,
            failure_code=failure_code,
            evidence_digest=self._takeover_digest(request, failure_code),
        )

    @staticmethod
    def _takeover_digest(request: ControlTakeoverRequest, evidence: object) -> str:
        canonical = json.dumps(
            {"request": request.model_dump(mode="json"), "evidence": str(evidence)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[SourceEvent]:
        async for event in self.provider.client.subscribe_state_events():
            yield StateChangedEvent(payload=event)

    async def health(self) -> AdapterHealth:
        connected = self._connected and await self.provider.client.health()
        return AdapterHealth(adapter_id=self.adapter_id, connected=connected)

    def _remember_sources(self, snapshot: AdapterSnapshot) -> None:
        self._source_by_command.clear()
        self._canonical_by_source.clear()
        canonical_by_source_device: dict[str, str] = {}
        used_ids: set[str] = set()
        for entity in snapshot.source_entities:
            entity_id = str(entity["entity_id"])
            source_device_id = str(entity.get("device_id") or entity_id)
            canonical_id = canonical_by_source_device.get(source_device_id)
            if canonical_id is None:
                explicit_canonical_id = entity.get("canonical_id")
                if explicit_canonical_id is not None:
                    canonical_id = str(explicit_canonical_id)
                else:
                    area_id = str(entity.get("area_id") or "unassigned")
                    name = str(entity.get("name") or entity_id)
                    base = f"{area_id}.{_slug(name)}"
                    canonical_id = base
                    suffix = 2
                    while canonical_id in used_ids:
                        canonical_id = f"{base}-{suffix}"
                        suffix += 1
                canonical_by_source_device[source_device_id] = canonical_id
                used_ids.add(canonical_id)
            self._canonical_by_source[entity_id] = canonical_id
            for capability in entity.get("capabilities", []):
                for command in capability.get("commands", []):
                    self._source_by_command.setdefault((canonical_id, str(command)), []).append(
                        entity_id
                    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "device"


__all__ = ["HomeAssistantProviderAdapter"]
