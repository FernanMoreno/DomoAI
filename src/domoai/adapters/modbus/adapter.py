"""Native Modbus adapter with a bounded semantic v1 profile."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from domoai.adapters.modbus.codec import encode_point
from domoai.adapters.modbus.config import (
    ModbusArea,
    ModbusCapabilityBinding,
    ModbusEntityConfig,
    ModbusMappingDocument,
    ModbusPoint,
    canonical_device_id,
    load_mapping,
)
from domoai.adapters.modbus.mapper import ModbusMapper
from domoai.adapters.modbus.transport import ModbusTransport, RawValue
from domoai.domain.models import (
    AdapterDiagnosticEvent,
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    AvailabilityChangedEvent,
    Command,
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext


class ModbusAdapter:
    """Adapt configured Modbus points into canonical DomoAI semantics."""

    adapter_id = "modbus"

    def __init__(
        self,
        transport: ModbusTransport,
        mapping: ModbusMappingDocument | Path,
        *,
        discovery_timeout: float = 5.0,
        poll_interval: float = 5.0,
        clock: Clock | None = None,
    ) -> None:
        self.transport = transport
        self.mapping = load_mapping(mapping) if isinstance(mapping, Path) else mapping
        self.discovery_timeout = discovery_timeout
        self.poll_interval = poll_interval
        self._clock = clock or SystemClock()
        self.mapper = ModbusMapper()
        self._connected = False
        self._available = False
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        # Best-effort, process-local duplicate suppression only -- reset on
        # restart, not shared across processes. The authoritative barrier
        # against re-executing a command is the persistent execution claim
        # in PlanRepository.claim_for_execution (Spec 057).
        self._executed_idempotency_keys: set[str] = set()
        self._canonical_by_source = {
            entity.entity_id: canonical_device_id(entity) for entity in self.mapping.entities
        }
        self._entity_by_canonical = {
            canonical_device_id(entity): entity for entity in self.mapping.entities
        }
        self._bindings_by_entity: dict[str, dict[str, ModbusCapabilityBinding]] = {}
        self._bindings_by_point: dict[
            tuple[int, ModbusArea, int],
            list[tuple[ModbusEntityConfig, ModbusCapabilityBinding]],
        ] = {}
        for entity in self.mapping.entities:
            self._bindings_by_entity[entity.entity_id] = {
                binding.name: binding for binding in entity.capabilities
            }
            for binding in entity.capabilities:
                point = binding.state
                key = (entity.unit_id, point.area, point.address)
                self._bindings_by_point.setdefault(key, []).append((entity, binding))

    async def connect(self) -> None:
        try:
            await self.transport.connect()
            self._connected = True
            self._available = await self.transport.health()
        except (ConnectionError, OSError, TimeoutError) as error:
            self._connected = False
            self._available = False
            raise ConnectionError("Modbus connection failed") from error

    async def disconnect(self) -> None:
        await self.transport.disconnect()
        self._connected = False
        self._available = False

    async def discover(self) -> AdapterSnapshot:
        self._require_connected()
        await self._poll()
        return self._snapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        self._require_connected()
        wanted = {source_ref.external_id for source_ref in source_refs}
        if wanted:
            await self._poll(wanted_entities=wanted)
        snapshots: list[StateSnapshot] = []
        for (entity_id, capability), state in sorted(self._states.items()):
            if entity_id not in wanted:
                continue
            snapshots.append(
                StateSnapshot(
                    device_id=self._canonical_by_source[entity_id],
                    capability=capability,
                    value=state["value"],
                    unit=state["unit"],
                    observed_at=state["observed_at"],
                    received_at=state["received_at"],
                    status=(
                        StateStatus.CURRENT
                        if state["available"] and self._available
                        else StateStatus.UNAVAILABLE
                    ),
                    source_ref=self._source_ref(entity_id),
                )
            )
        return snapshots

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        self._require_connected()
        entity = self._entity_by_canonical.get(command.device_id)
        if entity is None:
            return AdapterExecutionAck(accepted=False, message="Unknown Modbus device")
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
            return AdapterExecutionAck(accepted=False, message="Unknown Modbus entity")
        if command.idempotency_key in self._executed_idempotency_keys:
            return AdapterExecutionAck(accepted=False, message="Duplicate idempotency key")
        self._available = await self.transport.health()
        if not self._available:
            return AdapterExecutionAck(accepted=False, message="Modbus device is unavailable")
        translated = self._translate_command(entity, command)
        if translated is None:
            return AdapterExecutionAck(
                accepted=False,
                message=f"Unsupported Modbus command: {command.command}",
            )
        point, values = translated
        try:
            await asyncio.wait_for(
                self.transport.write(
                    entity.unit_id,
                    point.area,
                    point.address,
                    values,
                    execution_context=execution_context,
                ),
                self.discovery_timeout,
            )
        except (ConnectionError, OSError, TimeoutError) as error:
            self._available = False
            raise ConnectionError("Modbus command failed") from error
        self._executed_idempotency_keys.add(command.idempotency_key)
        return AdapterExecutionAck(
            accepted=True,
            source_ref=self._source_ref(entity.entity_id),
            message="Modbus command accepted",
        )

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        self._require_connected()
        while self._connected:
            for event in await self._poll():
                yield event
            if not self._connected:
                return
            await asyncio.sleep(self.poll_interval)

    async def health(self) -> AdapterHealth:
        connected = self._connected and await self.transport.health()
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=connected,
            message=None if connected else "Modbus transport is unavailable",
        )

    async def _poll(self, wanted_entities: set[str] | None = None) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        transport_available = await self.transport.health()
        if transport_available != self._available:
            self._available = transport_available
            events.append(
                AvailabilityChangedEvent(
                    payload={"available": self._available},
                )
            )
        if not transport_available:
            return events

        changed_entities: set[str] = set()
        changed_capabilities: set[str] = set()
        for key, bindings in self._bindings_by_point.items():
            selected = [
                (entity, binding)
                for entity, binding in bindings
                if wanted_entities is None or entity.entity_id in wanted_entities
            ]
            if not selected:
                continue
            unit_id, area, address = key
            point = selected[0][1].state
            try:
                sample = await asyncio.wait_for(
                    self.transport.read(unit_id, area, address, point.register_count),
                    self.discovery_timeout,
                )
                if sample is None:
                    raise ValueError("Modbus point returned no value")
                for entity, binding in selected:
                    decoded = self.mapper.decode(entity, binding, sample)
                    state_key = (entity.entity_id, binding.name)
                    previous = self._states.get(state_key)
                    received_at = self._clock.now()
                    self._states[state_key] = {
                        **decoded,
                        "received_at": received_at,
                        "available": True,
                    }
                    if previous is None or previous["value"] != decoded["value"]:
                        changed_entities.add(entity.entity_id)
                        changed_capabilities.add(binding.name)
                    elif previous.get("available") is False:
                        changed_entities.add(entity.entity_id)
                        changed_capabilities.add(binding.name)
            except (ConnectionError, OSError, TimeoutError, ValueError) as error:
                events.append(self._diagnostic(selected[0][0].entity_id, str(error)))
                for entity, binding in selected:
                    state = self._states.get((entity.entity_id, binding.name))
                    if state is not None:
                        state["available"] = False
        if changed_entities:
            events.append(
                StateChangedEvent(
                    payload={
                        "entity_ids": sorted(changed_entities),
                        "capabilities": sorted(changed_capabilities),
                    },
                )
            )
        return events

    def _snapshot(self) -> AdapterSnapshot:
        states = [
            {
                "entity_id": entity_id,
                "capability": capability,
                "value": state["value"],
                "unit": state["unit"],
                "available": state["available"] and self._available,
            }
            for (entity_id, capability), state in sorted(self._states.items())
        ]
        return self.mapper.to_snapshot(self.mapping, states=states, available=self._available)

    def _translate_command(
        self, entity: ModbusEntityConfig, command: Command
    ) -> tuple[ModbusPoint, tuple[RawValue, ...]] | None:
        bindings = self._bindings_by_entity[entity.entity_id]
        if command.command in {"turn_on", "turn_off"}:
            binding = bindings.get("power")
            if (
                binding is None
                or binding.command is None
                or command.unit is not None
                or command.value is not None
            ):
                return None
            return binding.command, encode_point(binding.command, command.command == "turn_on")
        if command.command == "set_brightness":
            binding = bindings.get("brightness")
            if (
                binding is None
                or binding.command is None
                or command.unit not in {None, "%"}
                or command.value is None
                or isinstance(command.value, bool)
                or not isinstance(command.value, (int, float))
                or not math.isfinite(float(command.value))
                or not 0 <= command.value <= 100
                or not float(command.value).is_integer()
            ):
                return None
            return binding.command, encode_point(binding.command, int(command.value))
        return None

    @staticmethod
    def _diagnostic(entity_id: str, reason: str) -> AdapterDiagnosticEvent:
        return AdapterDiagnosticEvent(
            payload={"entity_id": entity_id, "reason": reason[:200]},
        )

    def _source_ref(self, entity_id: str) -> SourceRef:
        entity = next(item for item in self.mapping.entities if item.entity_id == entity_id)
        return SourceRef(
            adapter_id=self.adapter_id,
            external_id=entity_id,
            external_type=f"modbus_unit:{entity.unit_id}",
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("Modbus adapter is not connected")
