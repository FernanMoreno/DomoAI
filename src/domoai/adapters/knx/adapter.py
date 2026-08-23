"""Native KNX adapter with a bounded semantic v1 profile."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
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
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext


class KnxAdapter:
    """Adapt configured KNX group addresses into canonical DomoAI semantics."""

    adapter_id = "knx"

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
        self._available = True
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
        self._available = await self.transport.health()

    async def disconnect(self) -> None:
        await self.transport.disconnect()
        self._connected = False
        self._available = False

    async def discover(self) -> AdapterSnapshot:
        self._require_connected()
        self._available = await self.transport.health()
        for group_address, bindings in self._bindings_by_state_address.items():
            dpt = bindings[0][1].dpt
            try:
                value = await asyncio.wait_for(
                    self.transport.read_group(group_address, dpt), self.discovery_timeout
                )
            except (ConnectionError, OSError, TimeoutError) as error:
                raise ConnectionError(f"KNX discovery failed: {error}") from error
            if value is None:
                continue
            try:
                self._ingest_value(value)
            except ValueError:
                continue
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
        self._available = await self.transport.health()
        for group_address in {binding.state_group_address for _entity, binding in bindings}:
            group_bindings = self._bindings_by_state_address[group_address]
            dpt = group_bindings[0][1].dpt
            try:
                value = await asyncio.wait_for(
                    self.transport.read_group(group_address, dpt), self.discovery_timeout
                )
            except (ConnectionError, OSError, TimeoutError) as error:
                raise ConnectionError(f"KNX state read failed: {error}") from error
            if value is not None:
                try:
                    self._ingest_value(value)
                except ValueError:
                    continue
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
        self._available = await self.transport.health()
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

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        self._require_connected()
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
            self._available = True
            event: SourceEvent | None
            try:
                event = self._ingest_value(value)
            except ValueError as error:
                event = self._diagnostic(value.group_address, str(error))
            if event is not None:
                yield event

    async def health(self) -> AdapterHealth:
        connected = self._connected and await self.transport.health()
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=connected,
            message=None if connected else "KNX transport is unavailable",
        )

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
        for state in decoded:
            key = (state["entity_id"], state["capability"])
            self._states[key] = {
                **state,
                "received_at": received_at,
                "available": self._available,
            }
            entity_ids.append(state["entity_id"])
            capabilities.append(state["capability"])
        return StateChangedEvent(
            payload={
                "entity_ids": sorted(set(entity_ids)),
                "capabilities": sorted(set(capabilities)),
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
        return None

    @staticmethod
    def _diagnostic(group_address: str, reason: str) -> AdapterDiagnosticEvent:
        return AdapterDiagnosticEvent(
            payload={"group_address": group_address, "reason": reason[:200]},
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("KNX adapter is not connected")
