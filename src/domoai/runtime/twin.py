"""Isolated preview of a plan against a snapshot of real device state."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Device, DeviceType, ExecutionSummary, Plan, PlanStatus, StrictModel
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore

_DOMAIN_FOR_TYPE: dict[DeviceType, str] = {
    DeviceType.LIGHT: "light",
    DeviceType.SWITCH: "switch",
    DeviceType.COVER: "cover",
}


class TwinSyncReport(StrictModel):
    mirrored_device_ids: list[str] = Field(default_factory=list)
    not_mirrored: list[dict[str, str]] = Field(default_factory=list)


async def _entity_for_device(device: Device, state_store: StateStore) -> dict[str, Any]:
    """Reverse-map a canonical Device into a raw entity dict SimulatedHomeAdapter accepts.

    Raises ValueError with a human-readable reason when the device's type or
    capabilities don't match the shapes HomeAssistantMapper/SimulatedHomeAdapter's
    _apply_command already understand (light/switch power+brightness, cover
    position) — mirroring only what has real, simulated command effects.
    """

    domain = _DOMAIN_FOR_TYPE.get(device.type)
    if domain is None:
        raise ValueError(f"unsupported device type: {device.type.value}")

    capability_names = {capability.name for capability in device.capabilities}
    state: dict[str, Any] = {}
    supported_features: list[str] = []
    attributes: dict[str, Any] = {}

    if domain in ("light", "switch"):
        if "power" not in capability_names:
            raise ValueError(f"{domain} missing expected power capability")
        power_snapshot = await state_store.get(device.id, "power")
        state["power"] = bool(power_snapshot.value) if power_snapshot else False
        if domain == "light" and "brightness" in capability_names:
            supported_features.append("brightness")
            brightness_capability = next(
                capability for capability in device.capabilities if capability.name == "brightness"
            )
            attributes["brightness_min"] = brightness_capability.minimum or 0
            attributes["brightness_max"] = brightness_capability.maximum or 100
            brightness_snapshot = await state_store.get(device.id, "brightness")
            state["brightness"] = brightness_snapshot.value if brightness_snapshot else 0
    elif domain == "cover":
        if "position" not in capability_names:
            raise ValueError("cover missing expected position capability")
        position_capability = next(
            capability for capability in device.capabilities if capability.name == "position"
        )
        attributes["position_min"] = position_capability.minimum or 0
        attributes["position_max"] = position_capability.maximum or 100
        position_snapshot = await state_store.get(device.id, "position")
        state["position"] = position_snapshot.value if position_snapshot else 0

    return {
        "entity_id": device.id,
        "domain": domain,
        "name": device.name,
        "area_id": device.area_id or "unassigned",
        "device_id": device.id,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "supported_features": supported_features,
        "attributes": attributes,
        "state": state,
    }


class DigitalTwin:
    """An isolated, on-demand mirror of real device state, safe to preview plans against."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self.plan_service: PlanService | None = None
        self.executor: PlanExecutor | None = None

    async def sync(
        self, live_registry: DeviceRegistry, live_state_store: StateStore
    ) -> TwinSyncReport:
        mirrored_entities: list[dict[str, Any]] = []
        mirrored_device_ids: list[str] = []
        not_mirrored: list[dict[str, str]] = []
        for device in live_registry.devices:
            try:
                entity = await _entity_for_device(device, live_state_store)
            except ValueError as error:
                not_mirrored.append({"device_id": device.id, "reason": str(error)})
                continue
            mirrored_entities.append(entity)
            mirrored_device_ids.append(device.id)

        adapter = SimulatedHomeAdapter(entities=mirrored_entities)
        registry = DeviceRegistry()
        state_store = StateStore()
        audit = AuditLog()
        await DiscoveryService(adapter, registry, state_store, audit).refresh()

        policy_engine = PolicyEngine([])
        self.plan_service = PlanService(
            registry, state_store, policy_engine, audit, clock=self.clock
        )
        self.executor = PlanExecutor(adapter, self.plan_service, audit, clock=self.clock)

        return TwinSyncReport(mirrored_device_ids=mirrored_device_ids, not_mirrored=not_mirrored)

    async def validate_and_execute(self, plan: Plan) -> ExecutionSummary:
        if self.plan_service is None or self.executor is None:
            self.plan_service = PlanService(
                DeviceRegistry(), StateStore(), PolicyEngine([]), AuditLog(), clock=self.clock
            )
            self.executor = PlanExecutor(
                SimulatedHomeAdapter(entities=[]), self.plan_service, AuditLog(), clock=self.clock
            )
        validated = self.plan_service.validate(plan)
        if validated.status is PlanStatus.READY:
            return await self.executor.execute(validated)
        return ExecutionSummary()
