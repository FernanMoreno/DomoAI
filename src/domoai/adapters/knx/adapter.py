"""Native KNX adapter with a bounded semantic v1 profile."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from domoai.adapters.knx.config import (
    KnxCapabilityBinding,
    KnxEntityConfig,
    KnxMappingDocument,
    canonical_device_id,
    load_mapping,
)
from domoai.adapters.knx.mapper import KnxMapper
from domoai.adapters.knx.transport import KnxGroupValue, KnxScalar, KnxTransport
from domoai.domain.models import (
    AdapterDiagnosticEvent,
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    AvailabilityChangedEvent,
    Command,
    ControlLeaseStatus,
    PhysicalBaseline,
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
    StateStatus,
    TakeoverResult,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.control_takeover import ControlTakeoverRequest
from domoai.runtime.execution_context import ExecutionContext


class KnxAdapter:
    """Adapt configured KNX group addresses into canonical DomoAI semantics."""

    adapter_id = "knx"
    # KNX state telegrams are consumed continuously by subscribe_events, but a
    # quiet bus emits no telegram for an unchanged value.  Periodic server-
    # owned reads are therefore required to maintain freshness evidence.
    state_events_are_authoritative = False
    inventory_is_static = True

    def __init__(
        self,
        transport: KnxTransport,
        mapping: KnxMappingDocument | Path,
        *,
        discovery_timeout: float = 5.0,
        clock: Clock | None = None,
    ) -> None:
        self.transport = transport
        self.mapping = load_mapping(mapping) if isinstance(mapping, Path) else mapping
        self.discovery_timeout = discovery_timeout
        self._clock = clock or SystemClock()
        self.mapper = KnxMapper()
        self._connected = False
        self._available = False
        self._event_stream_active = False
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self._canonical_by_source = {
            entity.entity_id: canonical_device_id(entity) for entity in self.mapping.entities
        }
        self._entity_by_canonical = {
            canonical: entity
            for entity, canonical in zip(
                self.mapping.entities, self._canonical_by_source.values(), strict=True
            )
        }
        self._bindings_by_state_address: dict[
            str, list[tuple[KnxEntityConfig, KnxCapabilityBinding]]
        ] = {}
        self._bindings_by_entity: dict[str, dict[str, KnxCapabilityBinding]] = {}
        for entity in self.mapping.entities:
            self._bindings_by_entity[entity.entity_id] = {
                binding.name: binding for binding in entity.capabilities
            }
            for binding in entity.capabilities:
                self._bindings_by_state_address.setdefault(binding.state_group_address, []).append(
                    (entity, binding)
                )
        # Best-effort, process-local duplicate suppression only -- reset on
        # restart, not shared across processes. The authoritative barrier
        # against re-executing a command is the persistent execution claim
        # in PlanRepository.claim_for_execution (Spec 057).
        self._executed_idempotency_keys: set[str] = set()

    async def connect(self) -> None:
        try:
            await self.transport.connect()
        except (ConnectionError, OSError, TimeoutError) as error:
            self._connected = False
            raise ConnectionError(f"KNX connection failed: {error}") from error
        self._connected = True
        # A KNX/IP tunnel only proves that the transport session exists.  It
        # does not prove that the configured bus/group addresses answer.
        # Discovery is the physical-availability admission point.
        self._available = False

    async def disconnect(self) -> None:
        await self.transport.disconnect()
        self._connected = False
        self._available = False

    async def discover(self) -> AdapterSnapshot:
        self._require_connected()
        self._available = False
        if not await self.transport.health():
            raise ConnectionError("KNX discovery failed: transport unavailable")
        responded = False
        failed_group_addresses: set[str] = set()
        unavailable_group_addresses: set[str] = set()
        for group_address, bindings in self._bindings_by_state_address.items():
            dpt = bindings[0][1].dpt
            try:
                value = await asyncio.wait_for(
                    self.transport.read_group(group_address, dpt), self.discovery_timeout
                )
            except (ConnectionError, OSError, TimeoutError):
                # A KNX installation may legitimately expose a configured
                # group address that does not answer GroupValueRead (for
                # example a virtual ETS object without the read flag).  Do
                # not let that single address hide evidence from the rest of
                # the bus.  The affected capabilities are emitted below as
                # unavailable, so state/readiness still fail closed for a
                # command that needs them.
                failed_group_addresses.add(group_address)
                continue
            if value is None:
                unavailable_group_addresses.add(group_address)
                continue
            try:
                self._ingest_value(value)
            except ValueError:
                continue
            responded = True
        self._available = responded
        for state in self._states.values():
            state["available"] = responded
        if not responded and failed_group_addresses:
            first_group = next(iter(failed_group_addresses))
            raise ConnectionError(f"KNX discovery failed: no group response for {first_group}")
        for group_address in unavailable_group_addresses | failed_group_addresses:
            for entity, binding in self._bindings_by_state_address[group_address]:
                self._states[(entity.entity_id, binding.name)] = {
                    "entity_id": entity.entity_id,
                    "capability": binding.name,
                    "value": None,
                    "unit": self.mapper.capability(binding)["unit"],
                    "available": False,
                    "observed_at": self._clock.now(),
                    "received_at": self._clock.now(),
                }
        return self._snapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        self._require_connected()
        wanted = {source_ref.external_id for source_ref in source_refs}
        bindings = [
            (entity, binding)
            for entity in self.mapping.entities
            if entity.entity_id in wanted
            for binding in entity.capabilities
        ]
        if not await self.transport.health():
            self._available = False
        cached_entities = {
            entity_id for entity_id, _capability in self._states if entity_id in wanted
        }
        for group_address in {binding.state_group_address for _entity, binding in bindings}:
            group_bindings = self._bindings_by_state_address[group_address]
            dpt = group_bindings[0][1].dpt
            try:
                value = await asyncio.wait_for(
                    self.transport.read_group(group_address, dpt), self.discovery_timeout
                )
            except (ConnectionError, OSError, TimeoutError) as error:
                # Some KNX devices (including KNX Virtual's basic functions)
                # publish a valid group-value response after a write but do
                # not answer a subsequent GroupValueRead.  The event stream
                # has already populated _states in that case, so preserve
                # that evidence while the transport itself remains healthy.
                # A read with no cached observation still fails closed.
                if not self._available or not cached_entities:
                    raise ConnectionError(f"KNX state read failed: {error}") from error
                continue
            if value is not None:
                try:
                    self._ingest_value(value)
                except ValueError:
                    continue
        return self._snapshots_for(wanted)

    def _snapshots_for(self, wanted: set[str]) -> list[StateSnapshot]:
        snapshots: list[StateSnapshot] = []
        for (entity_id, capability), state in self._states.items():
            if entity_id not in wanted:
                continue
            observed_at = state["observed_at"]
            received_at = state["received_at"]
            snapshots.append(
                StateSnapshot(
                    device_id=self._canonical_by_source[entity_id],
                    capability=capability,
                    value=state["value"],
                    unit=state["unit"],
                    observed_at=observed_at,
                    received_at=received_at,
                    status=(StateStatus.CURRENT if self._available else StateStatus.UNAVAILABLE),
                    source_ref=SourceRef(adapter_id=self.adapter_id, external_id=entity_id),
                )
            )
        return snapshots

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        self._require_connected()
        entity = self._entity_by_canonical.get(command.device_id)
        if entity is None:
            return AdapterExecutionAck(accepted=False, message="Unknown KNX device")
        return await self.execute_source(command, entity.entity_id, execution_context)

    async def execute_source(
        self,
        command: Command,
        source_entity_id: str,
        execution_context: ExecutionContext | None = None,
    ) -> AdapterExecutionAck:
        self._require_connected()
        entity = next(
            (item for item in self.mapping.entities if item.entity_id == source_entity_id),
            None,
        )
        if entity is None:
            return AdapterExecutionAck(accepted=False, message="Unknown KNX entity")
        if command.idempotency_key in self._executed_idempotency_keys:
            return AdapterExecutionAck(accepted=False, message="Duplicate idempotency key")
        if not await self.transport.health():
            self._available = False
        if not self._available:
            return AdapterExecutionAck(accepted=False, message="KNX device is unavailable")
        translated = self._translate_command(entity, command)
        if translated is None:
            return AdapterExecutionAck(
                accepted=False,
                message=f"Unsupported KNX command: {command.command}",
            )
        group_address, dpt, value = translated
        try:
            await self.transport.write_group(
                group_address, dpt, value, execution_context=execution_context
            )
        except (ConnectionError, OSError, TimeoutError) as error:
            self._available = False
            raise ConnectionError(f"KNX command failed: {error}") from error
        self._executed_idempotency_keys.add(command.idempotency_key)
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=entity.entity_id),
            message="KNX command accepted",
        )

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        """Return a bounded lab takeover based on a fresh KNX power baseline.

        KNX group communication has no portable ownership primitive. This
        method therefore does not claim to disable a native inverter
        scheduler; it only supports the explicit lab binding when the policy
        already says that native ownership is inactive/disabled. A physical
        provider must replace this handshake with its own ownership contract
        before production battery authority is enabled.
        """

        now = self._clock.now()
        entity = self._entity_by_canonical.get(request.device_id)
        if entity is None:
            return self._takeover_rejected(request, now, "battery_device_not_found")
        if request.native_scheduler_status in {"active", "unknown"} and not (
            request.allow_native_takeover
        ):
            return self._takeover_rejected(
                request,
                now,
                (
                    "native_owner_active"
                    if request.native_scheduler_status == "active"
                    else "native_owner_unknown"
                ),
            )

        binding = self._bindings_by_entity[entity.entity_id].get("battery.power")
        if binding is None or binding.command_group_address is None:
            return self._takeover_rejected(request, now, "battery_binding_not_found")
        if request.first_command not in {
            "charge_battery",
            "discharge_battery",
            "stop_battery",
        }:
            return self._takeover_rejected(request, now, "battery_command_not_found")
        if not self._available or not await self.transport.health():
            return self._takeover_rejected(request, now, "baseline_unavailable")

        try:
            feedback = await asyncio.wait_for(
                self.transport.read_group(binding.state_group_address, binding.dpt),
                self.discovery_timeout,
            )
        except (ConnectionError, OSError, TimeoutError):
            self._available = False
            return self._takeover_rejected(request, now, "baseline_unavailable")
        if feedback is None or isinstance(feedback.value, bool) or not isinstance(
            feedback.value, (int, float)
        ):
            return self._takeover_rejected(request, now, "baseline_unavailable")
        power_kw = float(feedback.value)
        if not math.isfinite(power_kw):
            return self._takeover_rejected(request, now, "baseline_invalid")

        baseline = PhysicalBaseline(
            device_id=request.device_id,
            capability="battery.power",
            power_kw=power_kw,
            observed_at=feedback.observed_at,
            received_at=max(now, feedback.observed_at),
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=entity.entity_id),
            state_revision=f"knx:{binding.state_group_address}:{feedback.observed_at.isoformat()}",
            native_scheduler_status=request.native_scheduler_status,
        )
        return TakeoverResult(
            lease_id=f"knx-control-{request.device_id}-{request.plan_id}",
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
        self,
        request: ControlTakeoverRequest,
        now: datetime,
        failure_code: str,
    ) -> TakeoverResult:
        return TakeoverResult(
            lease_id=f"knx-control-{request.device_id}-{request.plan_id}",
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
    def _takeover_digest(
        request: ControlTakeoverRequest, evidence: PhysicalBaseline | str
    ) -> str:
        payload = {
            "owner": request.owner,
            "device_id": request.device_id,
            "plan_id": request.plan_id,
            "first_command_id": request.first_command_id,
            "first_command": request.first_command,
            "evidence": (
                evidence.model_dump(mode="json")
                if isinstance(evidence, PhysicalBaseline)
                else evidence
            ),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        self._require_connected()
        self._event_stream_active = True
        try:
            while True:
                try:
                    value = await self.transport.receive(1.0)
                except (ConnectionError, OSError, TimeoutError) as error:
                    self._available = False
                    raise ConnectionError(f"KNX event stream failed: {error}") from error
                if value is None:
                    if not await self.transport.health() and self._available:
                        self._available = False
                        yield AvailabilityChangedEvent(
                            payload={"available": False},
                        )
                        return
                    # A healthy KNX bus may simply have no telegram during the
                    # polling interval.  Keep the subscription alive; treating
                    # idle time as stream termination makes the composite mark a
                    # perfectly healthy route unavailable and reconnect forever.
                    continue
                event: SourceEvent | None
                try:
                    event = self._ingest_value(value)
                except ValueError as error:
                    event = self._diagnostic(value.group_address, str(error))
                else:
                    # A valid, mapped telegram is physical bus evidence.  It
                    # may be the only evidence available for KNX Virtual or
                    # devices that publish state but do not answer reads.
                    self._available = True
                    for state in self._states.values():
                        state["available"] = True
                if event is not None:
                    yield event
        finally:
            self._event_stream_active = False

    async def health(self) -> AdapterHealth:
        connected = self._connected and self._available and await self.transport.health()
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=connected,
            message=None if connected else "KNX bus is unavailable",
        )

    def _snapshot(self) -> AdapterSnapshot:
        states = [
            {
                "entity_id": entity_id,
                "capability": capability,
                "value": state["value"],
                "unit": state["unit"],
                "available": state["available"] and self._available,
                "observed_at": state["observed_at"],
                "received_at": state["received_at"],
            }
            for (entity_id, capability), state in sorted(self._states.items())
        ]
        return self.mapper.to_snapshot(
            self.mapping,
            states=states,
            available=self._available,
        )

    def _ingest_value(self, value: KnxGroupValue) -> StateChangedEvent | None:
        decoded = self.mapper.decode_many(self.mapping, value)
        received_at = self._clock.now()
        entity_ids: list[str] = []
        capabilities: list[str] = []
        event_states: list[dict[str, Any]] = []
        for state in decoded:
            key = (state["entity_id"], state["capability"])
            self._states[key] = {
                **state,
                "received_at": received_at,
                "available": self._available,
            }
            entity_ids.append(state["entity_id"])
            capabilities.append(state["capability"])
            event_states.append(
                {
                    "device_id": self._canonical_by_source[state["entity_id"]],
                    "capability": state["capability"],
                    "value": state["value"],
                    "unit": state["unit"],
                    "observed_at": state["observed_at"],
                    # The adapter has received this telegram now. Preserve
                    # the source observation time while carrying receipt
                    # evidence through the event without another bus read.
                    "received_at": max(received_at, state["observed_at"]),
                    "status": "current" if state.get("available", True) else "unavailable",
                    "source_ref": {
                        "adapter_id": self.adapter_id,
                        "external_id": state["entity_id"],
                    },
                }
            )
        return StateChangedEvent(
            source_adapter_id=self.adapter_id,
            occurred_at=value.observed_at,
            capabilities=sorted(set(capabilities)),
            payload={
                "entity_ids": sorted(set(entity_ids)),
                "capabilities": sorted(set(capabilities)),
                # State-authoritative adapters must carry the observation in
                # the event. The consumer can persist it without triggering
                # a GroupValueRead -> GroupValueResponse -> event loop.
                "states": event_states,
            },
        )

    def _translate_command(
        self, entity: KnxEntityConfig, command: Command
    ) -> tuple[str, str, KnxScalar] | None:
        bindings = self._bindings_by_entity[entity.entity_id]
        if command.command in {"turn_on", "turn_off"}:
            binding = bindings.get("power")
            if binding is None or binding.command_group_address is None or command.unit is not None:
                return None
            return (
                binding.command_group_address,
                binding.dpt,
                command.command == "turn_on",
            )
        if command.command == "set_brightness":
            binding = bindings.get("brightness")
            if (
                binding is None
                or binding.command_group_address is None
                or command.unit not in {None, "%"}
                or command.value is None
                or isinstance(command.value, bool)
                or not isinstance(command.value, (int, float))
                or not 0 <= command.value <= 100
                or not float(command.value).is_integer()
            ):
                return None
            return (
                binding.command_group_address,
                binding.dpt,
                int(command.value),
            )
        if command.command in {"charge_battery", "discharge_battery", "stop_battery"}:
            binding = bindings.get("battery.power")
            if binding is None or binding.command_group_address is None:
                return None
            if command.command == "stop_battery":
                if command.unit not in {None, "kW"}:
                    return None
                if command.value is not None and (
                    isinstance(command.value, bool)
                    or not isinstance(command.value, (int, float))
                    or not math.isfinite(float(command.value))
                    or float(command.value) != 0.0
                ):
                    return None
                value = 0.0
            else:
                if (
                    command.value is None
                    or isinstance(command.value, bool)
                    or not isinstance(command.value, (int, float))
                    or not math.isfinite(float(command.value))
                    or command.value <= 0
                    or command.unit not in {None, "kW"}
                ):
                    return None
                value = float(command.value)
                if command.command == "discharge_battery":
                    value = -value
            return binding.command_group_address, binding.dpt, value
        return None

    @staticmethod
    def _diagnostic(group_address: str, reason: str) -> AdapterDiagnosticEvent:
        return AdapterDiagnosticEvent(
            payload={"group_address": group_address, "reason": reason[:200]},
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("KNX adapter is not connected")
