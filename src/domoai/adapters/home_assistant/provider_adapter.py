"""AdapterPort bridge for the Home Assistant Provider SDK implementation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import Literal, cast

from domoai.adapters.home_assistant.config import (
    HomeAssistantDispatchableBatteryBinding,
)
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.domain.energy import DispatchableBatteryBinding, EVChargingBinding
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

    def __init__(
        self,
        provider: HomeAssistantProvider,
        *,
        clock: Clock | None = None,
        dispatchable_battery_binding: DispatchableBatteryBinding | None = None,
        ev_charging_bindings: tuple[EVChargingBinding, ...] = (),
    ) -> None:
        self.provider = provider
        self._clock = clock or SystemClock()
        self._connected = False
        self._source_by_command: dict[tuple[str, str], list[str]] = {}
        self._canonical_by_source: dict[str, str] = {}
        self._dispatchable_battery_binding = dispatchable_battery_binding
        self._ev_charging_bindings = tuple(ev_charging_bindings)

    def bind_dispatchable_battery(self, binding: DispatchableBatteryBinding) -> None:
        """Attach the exact runtime binding used for physical takeover."""

        if binding.provider_id != self.provider.manifest.provider_id:
            raise ValueError("battery binding provider does not match Home Assistant")
        self._dispatchable_battery_binding = binding

    async def connect(self) -> None:
        await self.provider.connect()
        self._connected = True

    async def disconnect(self) -> None:
        if self._connected:
            await self.provider.disconnect()
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        provider_snapshot = await self.provider.snapshot()
        if self._ev_charging_bindings:
            self.provider.validate_ev_charging_routes(provider_snapshot)
        snapshot = self._project_semantic_snapshot(provider_snapshot)
        self._remember_sources(snapshot)
        return snapshot

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        wanted = {source_ref.external_id for source_ref in source_refs}
        snapshot = self._project_semantic_snapshot(await self.provider.snapshot())
        now = self._clock.now()
        states: list[StateSnapshot] = []
        for state in snapshot.source_states:
            if str(state["entity_id"]) not in wanted:
                continue
            observed_at = _parse_source_timestamp(state.get("observed_at"), fallback=now)
            received_at = max(
                observed_at,
                _parse_source_timestamp(state.get("received_at"), fallback=now),
            )
            states.append(
                StateSnapshot(
                    device_id=str(state["entity_id"]),
                    capability=str(state["capability"]),
                    value=state.get("value"),
                    unit=state.get("unit"),
                    observed_at=observed_at,
                    received_at=received_at,
                    status=(
                        StateStatus.CURRENT
                        if state.get("available", True)
                        else StateStatus.UNAVAILABLE
                    ),
                    source_ref=SourceRef(
                        adapter_id=self.adapter_id,
                        external_id=str(state["entity_id"]),
                    ),
                )
            )
        return states

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
        snapshot = self._project_semantic_snapshot(await self.provider.snapshot())
        self._remember_sources(snapshot)
        binding = self._matching_dispatch_binding(request)
        if binding is None:
            return self._takeover_rejected(request, now, "battery_binding_not_found")
        if request.native_scheduler_status == "active":
            return self._takeover_rejected(
                request, now, "native_scheduler_disable_not_configured"
            )
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
        observed_at = _parse_source_timestamp(state.get("observed_at"), fallback=now)
        received_at = max(
            now,
            _parse_source_timestamp(state.get("received_at"), fallback=now),
        )
        baseline = PhysicalBaseline(
            device_id=request.device_id,
            capability=binding.power_feedback_capability,
            power_kw=float(value),
            observed_at=observed_at,
            received_at=received_at,
            source_ref=SourceRef(
                adapter_id=self.adapter_id,
                external_id=binding.power_feedback_entity_id,
            ),
            state_revision=f"ha:{observed_at.isoformat()}",
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

    def _matching_dispatch_binding(
        self, request: ControlTakeoverRequest
    ) -> HomeAssistantDispatchableBatteryBinding | None:
        """Resolve one static HA route set for the exact canonical binding."""

        runtime_binding = self._dispatchable_battery_binding
        candidates = list(self.provider.battery_dispatch_bindings.values())
        if runtime_binding is None:
            candidates = [item for item in candidates if item.device_id == request.device_id]
        else:
            if request.device_id != runtime_binding.device_id:
                return None
            actuator = runtime_binding.profile.actuator
            if actuator is None:
                return None
            soc_observation = runtime_binding.profile.initial_soc_observation
            capacity_source = runtime_binding.capacity_evidence.source_ref
            candidates = [
                item
                for item in candidates
                if item.control_capability == actuator.capability
                and item.charge.provider_command == actuator.charge_command
                and item.discharge.provider_command == actuator.discharge_command
                and item.stop.provider_command == actuator.stop_command
                and item.power_feedback_capability == actuator.power_feedback_capability
                and (
                    soc_observation is None
                    or soc_observation.source_ref.external_id == item.soc_entity_id
                )
                and (
                    capacity_source is None
                    or capacity_source.external_id == item.capacity_entity_id
                )
            ]
        if len(candidates) != 1:
            return None
        return candidates[0]

    def _project_semantic_snapshot(self, snapshot: AdapterSnapshot) -> AdapterSnapshot:
        """Project provider metric mappings into the runtime AdapterPort view."""

        mapping_actuator_names = {
            item.control_capability
            for item in self.provider.battery_dispatch_bindings.values()
        }
        mapping_actuator_names.update(
            {
                binding.power_feedback_capability
                for binding in self.provider.ev_charging_bindings.values()
            }
        )
        active_actuator_names = set()
        if self._dispatchable_battery_binding is not None:
            actuator = self._dispatchable_battery_binding.profile.actuator
            if actuator is not None:
                active_actuator_names.add(actuator.capability)
        active_actuator_names.update(
            binding.actuator.capability
            for binding in self._ev_charging_bindings
            if binding.provider_id == self.provider.manifest.provider_id
        )
        explicit_sensor_mapping = bool(
            self.provider.metric_mappings or self.provider.battery_capacity_bindings
        )
        entities: list[dict[str, object]] = []
        for raw_entity in snapshot.source_entities:
            entity = dict(raw_entity)
            entity_id = str(entity["entity_id"])
            capabilities: list[dict[str, object]] = []
            for raw_capability in entity.get("capabilities", []):
                capability = dict(raw_capability)
                raw_name = str(capability["name"])
                semantic_name = self.provider.semantic_capability(entity_id, raw_name)
                if (
                    semantic_name is None
                    and explicit_sensor_mapping
                    and str(entity.get("domain")) == "sensor"
                ):
                    continue
                capability["name"] = semantic_name or raw_name
                if raw_name not in mapping_actuator_names or raw_name in active_actuator_names:
                    capabilities.append(capability)
            if (
                explicit_sensor_mapping
                and str(entity.get("domain")) == "sensor"
                and not capabilities
            ):
                continue
            entity["capabilities"] = capabilities
            entities.append(entity)

        states: list[dict[str, object]] = []
        for raw_state in snapshot.source_states:
            state = dict(raw_state)
            entity_id = str(state["entity_id"])
            raw_name = str(state["capability"])
            semantic_name = self.provider.semantic_capability(entity_id, raw_name)
            if semantic_name is None and explicit_sensor_mapping:
                continue
            state["capability"] = semantic_name or raw_name
            states.append(state)
        return snapshot.model_copy(update={"source_entities": entities, "source_states": states})

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


def _parse_source_timestamp(value: object, *, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return fallback
    return parsed.astimezone(fallback.tzinfo)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "device"


__all__ = ["HomeAssistantProviderAdapter"]
