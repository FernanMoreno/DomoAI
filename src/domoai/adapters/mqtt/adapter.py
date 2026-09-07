"""Generic MQTT adapter with declared-topic discovery only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import cast

from domoai.adapters.mqtt.codec import decode_json_value, validate_scalar_value
from domoai.adapters.mqtt.config import MqttAdapterMapping
from domoai.adapters.mqtt.mapper import MqttMapper
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    ScalarValue,
    SourceEvent,
    SourceRef,
    StateChangedEvent,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.mqtt_transport import MqttMessage, MqttTransport


class GenericMqttAdapter:
    def __init__(
        self,
        transport: MqttTransport,
        mapping: MqttAdapterMapping,
        *,
        clock: Clock | None = None,
        discovery_timeout: float = 5.0,
    ) -> None:
        self.transport = transport
        self.mapping = mapping
        self.adapter_id = mapping.adapter_id
        self.clock = clock or SystemClock()
        self.discovery_timeout = discovery_timeout
        self._connected = False
        self._states: dict[tuple[str, str], dict[str, object]] = {}
        self._executed_idempotency_keys: set[str] = set()

    async def connect(self) -> None:
        await self.transport.connect()
        for device in self.mapping.devices:
            for capability in device.capabilities:
                await self.transport.subscribe(capability.state_topic)
                if (
                    capability.feedback_topic
                    and capability.feedback_topic != capability.state_topic
                ):
                    await self.transport.subscribe(capability.feedback_topic)
        self._connected = True

    async def disconnect(self) -> None:
        await self.transport.disconnect()
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        self._require_connected()
        deadline = asyncio.get_running_loop().time() + self.discovery_timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            message = await self.transport.receive(min(remaining, 0.25))
            if message is None:
                break
            self._ingest(message)
        snapshot = MqttMapper().to_snapshot(self.mapping)
        snapshot.source_states = list(self._states.values())
        return snapshot

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        self._require_connected()
        wanted = {source_ref.external_id for source_ref in source_refs}
        now = self.clock.now()
        return [
            StateSnapshot(
                device_id=str(state["entity_id"]),
                capability=str(state["capability"]),
                value=cast(ScalarValue, state["value"]),
                unit=state["unit"],  # type: ignore[arg-type]
                observed_at=state["observed_at"],  # type: ignore[arg-type]
                received_at=now,
                status=StateStatus.CURRENT,
                source_ref=SourceRef(
                    adapter_id=self.adapter_id, external_id=str(state["entity_id"])
                ),
            )
            for state in self._states.values()
            if str(state["entity_id"]) in wanted
        ]

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        self._require_connected()
        if command.idempotency_key in self._executed_idempotency_keys:
            return AdapterExecutionAck(
                accepted=True,
                source_ref=SourceRef(adapter_id=self.adapter_id, external_id=command.device_id),
                message="duplicate MQTT command suppressed",
            )
        for device in self.mapping.devices:
            if device.source_id != command.device_id:
                continue
            for capability in device.capabilities:
                commands = capability.commands or [capability.name]
                if command.command in commands and capability.command_topic is not None:
                    if command.unit is not None and command.unit != capability.unit:
                        raise ValueError("MQTT command unit does not match mapping")
                    validate_scalar_value(
                        command.value,
                        kind=capability.kind,
                        minimum=capability.minimum,
                        maximum=capability.maximum,
                        enum_values=capability.enum_values,
                    )
                    payload = json.dumps(command.value, separators=(",", ":")).encode("utf-8")
                    await self.transport.publish(
                        capability.command_topic, payload, execution_context=execution_context
                    )
                    if capability.require_readback:
                        feedback = await self.transport.receive(0.1)
                        if feedback is None or feedback.topic != capability.feedback_topic:
                            return AdapterExecutionAck(
                                accepted=False,
                                source_ref=SourceRef(
                                    adapter_id=self.adapter_id, external_id=device.source_id
                                ),
                                message="generic MQTT readback was not received",
                            )
                        self._ingest(feedback)
                        observed = self._states.get((device.source_id, capability.name), {}).get(
                            "value"
                        )
                        if observed != command.value:
                            return AdapterExecutionAck(
                                accepted=False,
                                source_ref=SourceRef(
                                    adapter_id=self.adapter_id, external_id=device.source_id
                                ),
                                message="generic MQTT readback did not match command",
                            )
                    self._executed_idempotency_keys.add(command.idempotency_key)
                    return AdapterExecutionAck(
                        accepted=True,
                        source_ref=SourceRef(
                            adapter_id=self.adapter_id, external_id=device.source_id
                        ),
                        message="generic MQTT command published",
                    )
        raise ValueError("command is not declared by the generic MQTT mapping")

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        self._require_connected()
        while True:
            message = await self.transport.receive(1.0)
            if message is not None:
                event = self._ingest(message)
                if event is not None:
                    yield event

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            adapter_id=self.adapter_id, connected=self._connected and await self.transport.health()
        )

    def _ingest(self, message: MqttMessage) -> SourceEvent | None:
        for device in self.mapping.devices:
            for capability in device.capabilities:
                if (
                    message.topic != capability.state_topic
                    and message.topic != capability.feedback_topic
                ):
                    continue
                try:
                    value = decode_json_value(
                        message.payload,
                        kind=capability.kind,
                        minimum=capability.minimum,
                        maximum=capability.maximum,
                        enum_values=capability.enum_values,
                    )
                except ValueError:
                    return None
                self._states[(device.source_id, capability.name)] = {
                    "entity_id": device.source_id,
                    "capability": capability.name,
                    "value": value,
                    "unit": capability.unit,
                    "available": True,
                    "observed_at": self.clock.now(),
                }
                return StateChangedEvent(
                    source_adapter_id=self.adapter_id,
                    external_id=device.source_id,
                    capability=capability.name,
                    value=value,
                    unit=capability.unit,
                    occurred_at=self.clock.now(),
                )
        return None

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("generic MQTT adapter is not connected")
