"""Matter Server WebSocket adapter with a bounded semantic v1 profile."""

from __future__ import annotations

import copy
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from domoai.adapters.matter.mapper import (
    HUMIDITY_CLUSTER,
    LEVEL_CONTROL_CLUSTER,
    OCCUPANCY_CLUSTER,
    ON_OFF_CLUSTER,
    TEMPERATURE_CLUSTER,
    MatterMapper,
)
from domoai.adapters.matter.transport import MatterTransport, MatterTransportMessage
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Capability,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
    StateStatus,
)


class MatterServerAdapter:
    """Adapt commissioned Matter Server nodes into canonical DomoAI semantics."""

    adapter_id = "matter"

    def __init__(
        self,
        transport: MatterTransport,
        *,
        discovery_timeout: float = 5.0,
    ) -> None:
        self.transport = transport
        self.discovery_timeout = discovery_timeout
        self.mapper = MatterMapper()
        self._connected = False
        self._nodes: dict[int, dict[str, Any]] = {}
        self._source_entities: dict[str, dict[str, Any]] = {}
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self._unsupported: list[dict[str, Any]] = []
        self._canonical_by_source: dict[str, str] = {}
        self._executed_idempotency_keys: set[str] = set()

    async def connect(self) -> None:
        try:
            await self.transport.connect()
        except (ConnectionError, OSError, TimeoutError) as error:
            raise ConnectionError(f"Matter Server connection failed: {error}") from error
        self._connected = True

    async def disconnect(self) -> None:
        await self.transport.disconnect()
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        self._require_connected()
        try:
            result = await self.transport.request("start_listening")
        except (ConnectionError, OSError, TimeoutError) as error:
            raise ConnectionError(f"Matter Server discovery failed: {error}") from error
        if not isinstance(result, list):
            raise ConnectionError("Matter Server start_listening did not return node snapshots")
        for node in result:
            self._ingest_node(node)
        self._remember_sources()
        return self._snapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        self._require_connected()
        wanted = {source_ref.external_id for source_ref in source_refs}
        snapshots: list[StateSnapshot] = []
        for (source_entity_id, capability), state in self._states.items():
            if source_entity_id not in wanted:
                continue
            received_at = state.get("received_at", datetime.now(UTC))
            snapshots.append(
                StateSnapshot(
                    device_id=self._canonical_by_source.get(source_entity_id, source_entity_id),
                    capability=capability,
                    value=state.get("value"),
                    unit=state.get("unit"),
                    observed_at=received_at,
                    received_at=received_at,
                    status=(
                        StateStatus.CURRENT
                        if state.get("available", False)
                        else StateStatus.UNAVAILABLE
                    ),
                    source_ref=SourceRef(
                        adapter_id=self.adapter_id,
                        external_id=source_entity_id,
                    ),
                )
            )
        return snapshots

    async def execute(self, command: Command) -> AdapterExecutionAck:
        self._require_connected()
        source_entity_id = next(
            (
                source
                for source, canonical in self._canonical_by_source.items()
                if canonical == command.device_id
            ),
            None,
        )
        if source_entity_id is None:
            return AdapterExecutionAck(accepted=False, message="Unknown Matter endpoint")
        return await self.execute_source(command, source_entity_id)

    async def execute_source(
        self, command: Command, source_entity_id: str
    ) -> AdapterExecutionAck:
        self._require_connected()
        if command.idempotency_key in self._executed_idempotency_keys:
            return AdapterExecutionAck(accepted=False, message="Duplicate idempotency key")
        entity = self._source_entities.get(source_entity_id)
        if entity is None or not entity.get("available", False):
            return AdapterExecutionAck(accepted=False, message="Matter endpoint is unavailable")
        translated = self._translate_command(entity, command)
        if translated is None:
            return AdapterExecutionAck(
                accepted=False,
                message=f"Unsupported Matter command: {command.command}",
            )
        try:
            await self.transport.request("device_command", translated)
        except (ConnectionError, OSError, TimeoutError) as error:
            raise ConnectionError(f"Matter Server command failed: {error}") from error
        self._executed_idempotency_keys.add(command.idempotency_key)
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=source_entity_id),
            message="Matter Server command accepted",
        )

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        self._require_connected()
        while True:
            message = await self.transport.receive(1.0)
            if message is None:
                continue
            event = self._ingest(message)
            if event is not None:
                yield event

    async def health(self) -> AdapterHealth:
        connected = self._connected and await self.transport.health()
        return AdapterHealth(adapter_id=self.adapter_id, connected=connected)

    def _snapshot(self) -> AdapterSnapshot:
        return AdapterSnapshot(
            source_entities=[
                self._source_entities[entity_id]
                for entity_id in sorted(self._source_entities)
            ],
            source_states=list(self._states.values()),
            unsupported_sources=list(self._unsupported),
        )

    def _ingest(self, message: MatterTransportMessage) -> SourceEvent | None:
        payload = message.payload
        event = payload.get("event")
        data = payload.get("data")
        if not isinstance(event, str):
            return self._diagnostic("Matter Server event is missing an event name")
        if event in {"node_added", "node_updated"}:
            if not isinstance(data, dict):
                return self._diagnostic(f"{event} data must be an object")
            node_id = data.get("node_id")
            previous = self._nodes.get(node_id) if isinstance(node_id, int) else None
            try:
                self._ingest_node(data)
            except ValueError:
                return self._diagnostic(f"{event} data is invalid")
            if previous is not None and previous.get("available") != data.get("available"):
                return SourceEvent(
                    kind="availability_changed",
                    payload={"node_id": node_id, "available": bool(data.get("available", False))},
                )
            return SourceEvent(kind="state_changed", payload={"node_id": node_id})
        if event == "node_removed":
            if isinstance(data, dict):
                node_id = data.get("node_id")
            else:
                node_id = data
            if isinstance(node_id, bool) or not isinstance(node_id, int):
                return self._diagnostic("node_removed data must contain an integer node_id")
            self._remove_node(node_id)
            return SourceEvent(kind="device_membership_changed", payload={"node_id": node_id})
        if event == "attribute_updated":
            return self._ingest_attribute_update(data)
        if event == "connection_lost":
            self._connected = False
            return SourceEvent(kind="availability_changed", payload={"available": False})
        return self._diagnostic(f"unsupported Matter Server event: {event}")

    def _ingest_node(self, node: dict[str, Any]) -> None:
        node_id = self._valid_node_id(node)
        self._nodes[node_id] = copy.deepcopy(node)
        prefix = f"node:{node_id}/endpoint:"
        self._source_entities = {
            source: entity
            for source, entity in self._source_entities.items()
            if not source.startswith(prefix)
        }
        self._states = {
            key: state for key, state in self._states.items() if not key[0].startswith(prefix)
        }
        self._unsupported = [
            source
            for source in self._unsupported
            if not str(source.get("entity_id", "")).startswith(prefix)
        ]
        snapshot = self.mapper.to_snapshot([node])
        self._source_entities.update(
            {str(entity["entity_id"]): entity for entity in snapshot.source_entities}
        )
        self._unsupported.extend(snapshot.unsupported_sources)
        states, _diagnostics = self.mapper.map_states(node)
        received_at = datetime.now(UTC)
        for state in states:
            state["received_at"] = received_at
            self._states[(str(state["entity_id"]), str(state["capability"]))] = state
        self._remember_sources()

    def _ingest_attribute_update(self, data: Any) -> SourceEvent:
        if not isinstance(data, list) or len(data) != 3:
            return self._diagnostic("attribute_updated data must be [node_id, path, value]")
        node_id, path, value = data
        if isinstance(node_id, bool) or not isinstance(node_id, int) or not isinstance(path, str):
            return self._diagnostic("attribute_updated data has invalid identity")
        node = self._nodes.get(node_id)
        if node is None:
            return self._diagnostic("attribute_updated references an unknown node")
        parts = path.split("/")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return self._diagnostic("attribute_updated path is invalid")
        candidate = copy.deepcopy(node)
        attributes = candidate.get("attributes")
        if not isinstance(attributes, dict):
            return self._diagnostic("Matter node attributes must be an object")
        attributes[path] = value
        states, diagnostics = self.mapper.map_states(candidate)
        endpoint_id, cluster_id, attribute_id = (int(part) for part in parts)
        capability = self._capability_for_path(cluster_id, attribute_id)
        target_entity = self.mapper.source_entity_id(node_id, endpoint_id)
        if target_entity not in self._source_entities:
            return self._diagnostic("attribute_updated references an unknown endpoint")
        target_state = next(
            (
                state
                for state in states
                if state["entity_id"] == target_entity and state["capability"] == capability
            ),
            None,
        )
        if diagnostics and target_state is None:
            return self._diagnostic("attribute_updated value is invalid")
        if target_state is None:
            return self._diagnostic("attribute_updated value is invalid")
        self._ingest_node(candidate)
        return SourceEvent(
            kind="state_changed",
            payload={"node_id": node_id, "path": path},
        )

    def _remove_node(self, node_id: int) -> None:
        self._nodes.pop(node_id, None)
        prefix = f"node:{node_id}/endpoint:"
        for source, entity in self._source_entities.items():
            if source.startswith(prefix):
                entity["available"] = False
        for key, state in self._states.items():
            if key[0].startswith(prefix):
                state["available"] = False
        self._unsupported = [
            source
            for source in self._unsupported
            if not str(source.get("entity_id", "")).startswith(prefix)
        ]
        self._remember_sources()

    def _remember_sources(self) -> None:
        self._canonical_by_source.clear()
        used_ids: set[str] = set()
        for source_entity_id, entity in sorted(self._source_entities.items()):
            base = f"unassigned.{self._slug(str(entity.get('name') or source_entity_id))}"
            candidate = base
            suffix = 2
            while candidate in used_ids:
                candidate = f"{base}-{suffix}"
                suffix += 1
            self._canonical_by_source[source_entity_id] = candidate
            used_ids.add(candidate)

    @staticmethod
    def _translate_command(entity: dict[str, Any], command: Command) -> dict[str, Any] | None:
        capabilities = [
            Capability.model_validate(capability)
            for capability in entity.get("capabilities", [])
        ]
        if command.command in {"turn_on", "turn_off", "toggle"}:
            power = next(
                (capability for capability in capabilities if capability.name == "power"),
                None,
            )
            if power is None or not power.writable or command.command not in power.commands:
                return None
            return {
                "node_id": int(str(entity["entity_id"]).split(":", 1)[1].split("/", 1)[0]),
                "endpoint_id": int(str(entity["entity_id"]).rsplit(":", 1)[1]),
                "cluster_id": 6,
                "command_name": {
                    "turn_on": "On",
                    "turn_off": "Off",
                    "toggle": "Toggle",
                }[command.command],
                "payload": {},
            }
        if command.command == "set_brightness":
            brightness = next(
                (capability for capability in capabilities if capability.name == "brightness"),
                None,
            )
            if brightness is None or not brightness.writable or command.value is None:
                return None
            if isinstance(command.value, bool) or not isinstance(command.value, (int, float)):
                return None
            if not 0 <= command.value <= 100:
                return None
            source = str(entity["entity_id"])
            return {
                "node_id": int(source.split(":", 1)[1].split("/", 1)[0]),
                "endpoint_id": int(source.rsplit(":", 1)[1]),
                "cluster_id": 8,
                "command_name": "MoveToLevelWithOnOff",
                "payload": {
                    "level": round(float(command.value) * 254 / 100),
                    "transitionTime": 0,
                    "optionsMask": 0,
                    "optionsOverride": 0,
                },
            }
        return None

    @staticmethod
    def _capability_for_path(cluster_id: int, attribute_id: int) -> str | None:
        if attribute_id != 0:
            return None
        return {
            ON_OFF_CLUSTER: "power",
            LEVEL_CONTROL_CLUSTER: "brightness",
            TEMPERATURE_CLUSTER: "temperature",
            HUMIDITY_CLUSTER: "humidity",
            OCCUPANCY_CLUSTER: "occupancy",
        }.get(cluster_id)

    @staticmethod
    def _valid_node_id(node: dict[str, Any]) -> int:
        value = node.get("node_id")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Matter node_id must be a non-negative integer")
        return value

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "device"

    @staticmethod
    def _diagnostic(reason: str) -> SourceEvent:
        return SourceEvent(kind="adapter_diagnostic", payload={"reason": reason[:200]})

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("Matter Server adapter is not connected")
