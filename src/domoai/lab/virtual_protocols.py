"""Protocol-independent adapter projections over :mod:`virtual_plant`.

The projection deliberately implements the established ``AdapterPort``
surface. It is used for the deterministic runtime matrix; native protocol
translation remains covered by the protocol-specific adapter suites and by the
optional process-level lab.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Capability,
    CapabilityKind,
    Command,
    DeviceType,
    SourceEvent,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.lab.virtual_plant import VirtualHomePlant, VirtualPlantDevice
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext

_ADAPTER_IDS = (
    "fixture",
    "home_assistant",
    "knx",
    "modbus",
    "matter",
    "zigbee2mqtt",
)

_BOOLEAN_CAPABILITIES = {"power", "occupancy", "ev.connected"}
_INTEGER_CAPABILITIES = {"brightness", "position"}
_ENUM_CAPABILITIES = {"thermal.hvac_mode"}
_UNITS = {
    "brightness": "%",
    "position": "%",
    "temperature": "°C",
    "target_temperature": "°C",
    "battery.soc": "%",
    "battery.power": "kW",
    "battery.capacity": "kWh",
    "ev.soc": "%",
    "ev_charging": "kW",
    "ev.capacity": "kWh",
    "water.flow_rate": "L/min",
    "water.total_volume": "L",
    "thermal.indoor_temperature": "°C",
    "thermal.hvac_power": "kW",
    "solar.power": "W",
}
_DEVICE_TYPES = {
    "light": DeviceType.LIGHT,
    "switch": DeviceType.SWITCH,
    "cover": DeviceType.COVER,
    "climate": DeviceType.CLIMATE,
    "sensor": DeviceType.SENSOR,
    "environment": DeviceType.SENSOR,
    "energy": DeviceType.ENERGY,
    "power": DeviceType.ENERGY,
    "battery": DeviceType.ENERGY,
    "ev": DeviceType.EV_CHARGER,
    "water": DeviceType.SENSOR,
    "thermal": DeviceType.CLIMATE,
    "solar": DeviceType.ENERGY,
}


def _capability_kind(name: str) -> CapabilityKind:
    if name in _BOOLEAN_CAPABILITIES:
        return CapabilityKind.BOOLEAN
    if name in _INTEGER_CAPABILITIES:
        return CapabilityKind.INTEGER
    if name in _ENUM_CAPABILITIES:
        return CapabilityKind.ENUM
    return CapabilityKind.NUMBER


class VirtualProtocolAdapter:
    """AdapterPort projection that routes all operations into one plant."""

    state_events_are_authoritative = False
    inventory_is_static = True

    def __init__(
        self, plant: VirtualHomePlant, adapter_id: str, *, clock: Clock | None = None
    ) -> None:
        if adapter_id not in _ADAPTER_IDS:
            raise ValueError(f"unknown virtual protocol adapter: {adapter_id}")
        self.plant = plant
        self.adapter_id = adapter_id
        self._clock = clock or plant.clock or SystemClock()
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        self._require_connected()
        self.plant.discover(self.adapter_id)
        entities = [entity for entity in self.plant.devices if entity.adapter_id == self.adapter_id]
        return AdapterSnapshot(
            source_entities=[self._entity(entity) for entity in entities],
            source_states=[state for entity in entities for state in self._states(entity)],
        )

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        self._require_connected()
        snapshots: list[StateSnapshot] = []
        for source_ref in source_refs:
            for entity in self.plant.devices:
                if (
                    entity.adapter_id != self.adapter_id
                    or entity.source_id != source_ref.external_id
                ):
                    continue
                for capability in entity.capabilities:
                    snapshots.append(self._state_snapshot(entity, capability))
        return snapshots

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        self._require_connected()
        device = next(
            (
                item
                for item in self.plant.devices
                if item.adapter_id == self.adapter_id and item.device_id == command.device_id
            ),
            None,
        )
        if device is None:
            return AdapterExecutionAck(accepted=False, message="Unknown virtual device")
        try:
            self.plant.command(
                self.adapter_id,
                device.source_id,
                command.command,
                value=command.value,
                idempotency_key=command.idempotency_key,
            )
        except ConnectionError:
            raise
        except ValueError as error:
            return AdapterExecutionAck(accepted=False, message=str(error))
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=device.source_id),
            message="Virtual protocol command accepted",
        )

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        self._require_connected()
        return
        yield cast(SourceEvent, None)  # pragma: no cover - async-generator contract

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self._connected)

    def _entity(self, entity: VirtualPlantDevice) -> dict[str, Any]:
        capabilities = [self._capability(entity, name) for name in entity.capabilities]
        return {
            "entity_id": entity.source_id,
            "device_id": entity.source_id,
            "canonical_id": entity.device_id,
            "local_canonical_id": entity.device_id,
            "domain": entity.domain,
            "semantic_type": _DEVICE_TYPES.get(entity.domain, DeviceType.UNSUPPORTED).value,
            "name": entity.device_id,
            "area_id": "virtual",
            "manufacturer": "DomoAI Digital Twin",
            "model": f"{self.adapter_id}-virtual",
            "identity_keys": [f"digital-twin:{entity.device_id}"],
            "connections": [f"digital-twin:{self.adapter_id}"],
            "capabilities": capabilities,
            "available": self.plant.entity(entity)["available"],
        }

    def _capability(self, entity: VirtualPlantDevice, name: str) -> dict[str, Any]:
        minimum = maximum = None
        if name in entity.bounds:
            minimum, maximum = entity.bounds[name]
            if maximum == float("inf"):
                maximum = None
        commands = [
            command for command, capability in entity.commands.items() if capability == name
        ]
        kind = _capability_kind(name)
        return Capability(
            name=name,
            kind=kind,
            unit=_UNITS.get(name),
            readable=True,
            writable=bool(commands),
            minimum=minimum,
            maximum=maximum,
            enum_values=["off", "heat", "cool"] if kind is CapabilityKind.ENUM else [],
            commands=commands,
        ).model_dump(mode="json")

    def _states(self, entity: VirtualPlantDevice) -> list[dict[str, Any]]:
        states: list[dict[str, Any]] = []
        for capability in entity.capabilities:
            observation = self.plant.read(self.adapter_id, entity.source_id, capability)
            states.append(
                {
                    "entity_id": entity.source_id,
                    "capability": capability,
                    "value": observation.value,
                    "unit": _UNITS.get(capability),
                    "available": observation.available,
                    "observed_at": observation.observed_at,
                    "received_at": self._clock.now(),
                }
            )
        return states

    def _state_snapshot(self, entity: VirtualPlantDevice, capability: str) -> StateSnapshot:
        observation = self.plant.read(self.adapter_id, entity.source_id, capability)
        if not observation.available:
            status = StateStatus.UNAVAILABLE
        elif observation.observed_at < self._clock.now():
            status = StateStatus.STALE
        else:
            status = StateStatus.CURRENT
        return StateSnapshot(
            device_id=entity.device_id,
            capability=capability,
            value=observation.value,
            unit=_UNITS.get(capability),
            observed_at=observation.observed_at,
            received_at=self._clock.now(),
            status=status,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=entity.source_id),
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError(f"virtual {self.adapter_id} adapter is not connected")


def build_virtual_protocol_adapters(
    plant: VirtualHomePlant, *, clock: Clock | None = None
) -> tuple[VirtualProtocolAdapter, ...]:
    """Build the complete adapter profile set required by the matrix."""

    return tuple(
        VirtualProtocolAdapter(plant, adapter_id, clock=clock) for adapter_id in _ADAPTER_IDS
    )


__all__ = ["VirtualProtocolAdapter", "build_virtual_protocol_adapters"]
