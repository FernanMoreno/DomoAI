"""Zigbee2MQTT adapter over the project-local MQTT transport boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from domoai.adapters.zigbee2mqtt.mapper import canonical_id, map_definition, map_states
from domoai.adapters.zigbee2mqtt.transport import MqttMessage, MqttTransport
from domoai.domain.models import (
    AdapterDiagnosticEvent,
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    AvailabilityChangedEvent,
    Command,
    DeviceMembershipChangedEvent,
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
    StateStatus,
)


class Zigbee2MqttAdapter:
    adapter_id = "zigbee2mqtt"

    def __init__(
        self,
        transport: MqttTransport,
        *,
        base_topic: str = "zigbee2mqtt",
        discovery_timeout: float = 5.0,
    ) -> None:
        self.transport = transport
        self.base_topic = base_topic.strip("/")
        self.discovery_timeout = discovery_timeout
        self._connected = False
        self._definitions: dict[str, dict[str, Any]] = {}
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self._availability: dict[str, bool] = {}
        self._bridge_online = True
        self._unsupported: list[dict[str, Any]] = []
        # Best-effort, process-local duplicate suppression only -- reset on
        # restart, not shared across processes. The authoritative barrier
        # against re-executing a command is the persistent execution claim
        # in PlanRepository.claim_for_execution (Spec 057).
        self._executed_idempotency_keys: set[str] = set()

    async def connect(self) -> None:
        try:
            await self.transport.connect()
            await self.transport.subscribe(f"{self.base_topic}/#")
        except (ConnectionError, OSError, TimeoutError) as error:
            raise ConnectionError(f"Zigbee2MQTT connection failed: {error}") from error
        self._connected = True

    async def disconnect(self) -> None:
        await self.transport.disconnect()
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        self._require_connected()
        deadline = asyncio.get_running_loop().time() + self.discovery_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            message = await self.transport.receive(remaining)
            if message is None:
                break
            self._ingest(message)
        return self._snapshot()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        self._require_connected()
        wanted = {source_ref.external_id for source_ref in source_refs}
        now = datetime.now(UTC)
        snapshots: list[StateSnapshot] = []
        for (friendly_name, capability), state in self._states.items():
            if friendly_name not in wanted:
                continue
            available = self._availability.get(friendly_name, self._bridge_online)
            received_at = state.get("received_at", now)
            snapshots.append(
                StateSnapshot(
                    device_id=canonical_id(friendly_name),
                    capability=capability,
                    value=state["value"],
                    unit=state.get("unit"),
                    observed_at=received_at,
                    received_at=received_at,
                    status=StateStatus.CURRENT if available else StateStatus.UNAVAILABLE,
                    source_ref=SourceRef(
                        adapter_id=self.adapter_id,
                        external_id=friendly_name,
                    ),
                )
            )
        return snapshots

    async def execute(self, command: Command) -> AdapterExecutionAck:
        self._require_connected()
        friendly_name = self._friendly_for_device(command.device_id)
        if friendly_name is None:
            return AdapterExecutionAck(accepted=False, message="Unknown Zigbee2MQTT device")
        return await self.execute_source(command, friendly_name)

    async def execute_source(self, command: Command, source_entity_id: str) -> AdapterExecutionAck:
        self._require_connected()
        friendly_name = source_entity_id
        if friendly_name not in self._definitions:
            return AdapterExecutionAck(accepted=False, message="Unknown Zigbee2MQTT entity")
        if command.idempotency_key in self._executed_idempotency_keys:
            return AdapterExecutionAck(accepted=False, message="Duplicate idempotency key")
        payload = self._command_payload(friendly_name, command)
        if payload is None:
            return AdapterExecutionAck(
                accepted=False,
                message=f"Unsupported Zigbee2MQTT command: {command.command}",
            )
        try:
            await self.transport.publish(
                f"{self.base_topic}/{friendly_name}/set",
                json.dumps(payload, separators=(",", ":")).encode(),
            )
        except (ConnectionError, OSError, TimeoutError) as error:
            raise ConnectionError(f"Zigbee2MQTT publish failed: {error}") from error
        self._executed_idempotency_keys.add(command.idempotency_key)
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=friendly_name),
            message="Zigbee2MQTT command published",
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
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=connected,
            message=None if self._bridge_online else "Zigbee2MQTT bridge is offline",
        )

    def _snapshot(self) -> AdapterSnapshot:
        entities = [
            map_definition(
                definition,
                available=self._availability.get(name, self._bridge_online),
            )
            for name, definition in sorted(self._definitions.items())
        ]
        source_states = [state for state in self._states.values()]
        return AdapterSnapshot(
            source_entities=entities,
            source_states=source_states,
            unsupported_sources=list(self._unsupported),
        )

    def _ingest(self, message: MqttMessage) -> SourceEvent | None:
        relative = self._relative_topic(message.topic)
        if relative == "bridge/devices":
            return self._ingest_devices(message)
        if relative == "bridge/state":
            return self._ingest_bridge_state(message)
        if relative == "bridge/event":
            return self._ingest_bridge_event(message)
        if relative.endswith("/availability"):
            friendly_name = relative[: -len("/availability")]
            return self._ingest_availability(friendly_name, message)
        if relative and not relative.startswith("bridge/"):
            return self._ingest_state(relative, message)
        return None

    def _ingest_devices(self, message: MqttMessage) -> SourceEvent | None:
        payload = self._json(message)
        if not isinstance(payload, list):
            self._unsupported.append({"topic": message.topic, "reason": "devices must be an array"})
            return self._diagnostic(message.topic, "bridge/devices payload must be an array")
        self._definitions.clear()
        self._unsupported.clear()
        for definition in payload:
            if not isinstance(definition, dict):
                continue
            friendly_name = str(definition.get("friendly_name") or "")
            if not friendly_name:
                continue
            self._definitions[friendly_name] = definition
            if not definition.get("supported", False):
                self._unsupported.append(
                    {"friendly_name": friendly_name, "reason": "unsupported definition"}
                )
        return DeviceMembershipChangedEvent(
            payload={"count": len(self._definitions)},
        )

    def _ingest_bridge_state(self, message: MqttMessage) -> SourceEvent | None:
        payload = self._json(message)
        if not isinstance(payload, dict) or payload.get("state") not in {"online", "offline"}:
            return self._diagnostic(message.topic, "bridge state must be online or offline")
        self._bridge_online = payload["state"] == "online"
        return AvailabilityChangedEvent(
            payload={"bridge": True, "available": self._bridge_online},
        )

    def _ingest_bridge_event(self, message: MqttMessage) -> SourceEvent | None:
        payload = self._json(message)
        if not isinstance(payload, dict):
            return self._diagnostic(message.topic, "bridge event must be an object")
        return DeviceMembershipChangedEvent(payload={"event": payload})

    def _ingest_availability(self, friendly_name: str, message: MqttMessage) -> SourceEvent:
        payload = self._json(message)
        if not isinstance(payload, dict) or payload.get("state") not in {"online", "offline"}:
            return self._diagnostic(message.topic, "availability state must be online or offline")
        available = payload["state"] == "online"
        self._availability[friendly_name] = available
        return AvailabilityChangedEvent(
            payload={"friendly_name": friendly_name, "available": available},
        )

    def _ingest_state(self, friendly_name: str, message: MqttMessage) -> SourceEvent:
        payload = self._json(message)
        if not isinstance(payload, dict):
            return self._diagnostic(message.topic, "device state must be an object")
        available = self._availability.get(friendly_name, self._bridge_online)
        states, diagnostics = map_states(friendly_name, payload, available=available)
        received_at = datetime.now(UTC)
        for state in states:
            state["received_at"] = received_at
            self._states[(friendly_name, state["capability"])] = state
        if diagnostics:
            return self._diagnostic(message.topic, "; ".join(diagnostics))
        return StateChangedEvent(
            payload={
                "friendly_name": friendly_name,
                "capabilities": [state["capability"] for state in states],
            },
        )

    def _json(self, message: MqttMessage) -> Any:
        try:
            return json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _diagnostic(self, topic: str, reason: str) -> AdapterDiagnosticEvent:
        return AdapterDiagnosticEvent(payload={"topic": topic, "reason": reason})

    def _relative_topic(self, topic: str) -> str:
        prefix = f"{self.base_topic}/"
        return topic[len(prefix) :] if topic.startswith(prefix) else ""

    def _friendly_for_device(self, device_id: str) -> str | None:
        for friendly_name in self._definitions:
            if canonical_id(friendly_name) == device_id:
                return friendly_name
        return None

    def _command_payload(self, friendly_name: str, command: Command) -> dict[str, Any] | None:
        entity = map_definition(
            self._definitions[friendly_name],
            available=self._availability.get(friendly_name, self._bridge_online),
        )
        commands = {
            command_name
            for capability in entity["capabilities"]
            for command_name in capability.get("commands", [])
        }
        if command.command not in commands:
            return None
        if command.command in {"turn_on", "turn_off", "toggle"}:
            state_values = {"turn_on": "ON", "turn_off": "OFF", "toggle": "TOGGLE"}
            return {"state": state_values[command.command]}
        if command.command == "set_brightness":
            if isinstance(command.value, bool) or not isinstance(command.value, (int, float)):
                return None
            if not 0 <= command.value <= 100:
                return None
            return {"brightness": round(float(command.value) * 254 / 100)}
        return None

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("Zigbee2MQTT adapter is not connected")
