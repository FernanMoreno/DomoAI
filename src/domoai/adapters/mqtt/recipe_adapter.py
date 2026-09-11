"""Adapter bridge from a declarative recipe to the generic MQTT adapter."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from domoai.adapters.mqtt.adapter import GenericMqttAdapter
from domoai.adapters.mqtt.config import (
    MqttAdapterMapping,
    MqttCapabilityMapping,
    MqttDeviceMapping,
)
from domoai.domain.connections import ConnectionCandidate
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
)
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.mqtt_transport import MqttTransport


class MqttRecipeAdapter:
    """Compile one recipe and selected candidate into GenericMqttAdapter."""

    def __init__(
        self,
        transport: MqttTransport,
        recipe: ConnectorRecipe,
        candidate: ConnectionCandidate,
        *,
        adapter_id: str,
    ) -> None:
        if recipe.transport_family.value != "mqtt":
            raise ValueError("MQTT recipe adapter requires the mqtt transport family")
        if candidate.connector_id != recipe.connector_id:
            raise ValueError("recipe and candidate connector IDs must match")
        mapping = self._mapping(recipe, candidate, adapter_id)
        self._delegate = GenericMqttAdapter(transport, mapping)
        self.adapter_id = adapter_id

    async def connect(self) -> None:
        await self._delegate.connect()

    async def disconnect(self) -> None:
        await self._delegate.disconnect()

    async def discover(self) -> AdapterSnapshot:
        return await self._delegate.discover()

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        return await self._delegate.read_state(source_refs)

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        return await self._delegate.execute(command, execution_context)

    async def execute_source(
        self,
        command: Command,
        source_entity_id: str,
        execution_context: ExecutionContext | None = None,
    ) -> AdapterExecutionAck:
        """Translate a canonical registry route back to the recipe source ID."""

        source_command = command.model_copy(update={"device_id": source_entity_id})
        return await self._delegate.execute(source_command, execution_context)

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        async for event in self._delegate.subscribe_events():
            yield event

    async def health(self) -> AdapterHealth:
        return await self._delegate.health()

    @classmethod
    def _mapping(
        cls, recipe: ConnectorRecipe, candidate: ConnectionCandidate, adapter_id: str
    ) -> MqttAdapterMapping:
        capabilities: list[MqttCapabilityMapping] = []
        for binding in recipe.capabilities:
            if binding.state_path is not None or binding.state_topic is None:
                raise ValueError("MQTT recipe capabilities require state_topic")
            command = binding.command
            if command is not None:
                if command.method.upper() != "PUBLISH":
                    raise ValueError("MQTT recipe commands must use PUBLISH")
                if command.command_topic is None or command.readback_topic is None:
                    raise ValueError("MQTT recipe commands require MQTT readback topics")
            capabilities.append(
                MqttCapabilityMapping(
                    name=binding.capability_id,
                    kind=binding.kind,
                    unit=binding.unit,
                    minimum=binding.minimum,
                    maximum=binding.maximum,
                    enum_values=list(binding.enum_values),
                    commands=[command.name] if command is not None else [],
                    state_topic=cls._render(binding.state_topic, candidate.candidate_id),
                    command_topic=(
                        cls._render(command.command_topic, candidate.candidate_id)
                        if command is not None and command.command_topic is not None
                        else None
                    ),
                    feedback_topic=(
                        cls._render(command.readback_topic, candidate.candidate_id)
                        if command is not None and command.readback_topic is not None
                        else None
                    ),
                    require_readback=command is not None,
                )
            )
        return MqttAdapterMapping(
            schema_version="v1",
            adapter_id=adapter_id,
            devices=[
                MqttDeviceMapping(
                    source_id=candidate.candidate_id,
                    type=recipe.device_type.value,
                    capabilities=capabilities,
                )
            ],
        )

    @staticmethod
    def _render(template: str, device_id: str) -> str:
        return template.replace("{device_id}", device_id)


class MqttRecipeTransport:
    """Recipe engine that compiles MQTT recipes into the generic adapter."""

    def __init__(self, transport: MqttTransport) -> None:
        self.transport = transport

    def build_adapter(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup, *, adapter_id: str
    ) -> MqttRecipeAdapter:
        return MqttRecipeAdapter(
            self.transport,
            recipe,
            setup.candidate,
            adapter_id=adapter_id,
        )


class MqttRecipeTransportFactory:
    """Build a fresh MQTT engine from the selected connection setup."""

    def __init__(
        self,
        factory: Callable[[ConnectionSetup], MqttTransport | Awaitable[MqttTransport]],
    ) -> None:
        self.factory = factory

    async def build_adapter(
        self, recipe: ConnectorRecipe, setup: ConnectionSetup, *, adapter_id: str
    ) -> MqttRecipeAdapter:
        result = self.factory(setup)
        resolved = await result if inspect.isawaitable(result) else result
        return MqttRecipeAdapter(
            resolved, recipe, setup.candidate, adapter_id=adapter_id
        )


__all__ = ["MqttRecipeAdapter", "MqttRecipeTransport", "MqttRecipeTransportFactory"]
