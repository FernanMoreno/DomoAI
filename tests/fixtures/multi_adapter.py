"""Deterministic multi-adapter source fixtures for Spec 008."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.execution_context import ExecutionContext


def entity(
    *,
    entity_id: str,
    source_device_id: str,
    canonical_id: str,
    name: str,
    capabilities: list[dict[str, Any]],
    area_id: str = "living_room",
    parent_device_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "entity_id": entity_id,
        "device_id": source_device_id,
        "canonical_id": canonical_id,
        "identity_keys": [f"fixture:device:{source_device_id}"],
        "connections": [f"fixture:{source_device_id}"],
        "name": name,
        "area_id": area_id,
        "domain": ("light" if any(item["name"] == "power" for item in capabilities) else "sensor"),
        "semantic_type": (
            "light" if any(item["name"] == "power" for item in capabilities) else "sensor"
        ),
        "manufacturer": "Fixture",
        "model": "Multi-Adapter",
        "capabilities": capabilities,
        "available": True,
    }
    if parent_device_id is not None:
        result["parent_device_id"] = parent_device_id
    return result


def power_capability() -> dict[str, Any]:
    return {
        "name": "power",
        "kind": "boolean",
        "readable": True,
        "writable": True,
        "commands": ["turn_on", "turn_off"],
    }


def brightness_capability() -> dict[str, Any]:
    return {
        "name": "brightness",
        "kind": "integer",
        "unit": "%",
        "readable": True,
        "writable": True,
        "minimum": 0,
        "maximum": 100,
        "commands": ["set_brightness"],
    }


def read_only_capability(name: str, unit: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "number" if name != "occupancy" else "boolean",
        "unit": unit,
        "readable": True,
        "writable": False,
    }


def source_snapshot(
    *,
    adapter_id: str,
    extra_entities: int = 0,
    include_shared_device: bool = True,
    shared_brightness: bool = False,
) -> AdapterSnapshot:
    entities: list[dict[str, Any]] = []
    if include_shared_device:
        entities.extend(
            [
                entity(
                    entity_id="light.main_power",
                    source_device_id="physical-light-1",
                    canonical_id="living_room.main_light",
                    name="Main Light",
                    capabilities=[power_capability()],
                ),
                entity(
                    entity_id="light.main_brightness",
                    source_device_id="physical-light-1",
                    canonical_id="living_room.main_light",
                    name="Main Light Brightness",
                    capabilities=[brightness_capability()],
                ),
            ]
        )
    entities.extend(
        [
            entity(
                entity_id=f"sensor.{adapter_id}.temperature",
                source_device_id=f"{adapter_id}-environment",
                canonical_id=f"{adapter_id}.environment",
                name=f"{adapter_id.title()} Environment",
                capabilities=[read_only_capability("temperature", "°C")],
                area_id="bedroom",
            ),
        ]
    )
    if shared_brightness:
        entities.append(
            entity(
                entity_id=f"{adapter_id}.main_brightness_backup",
                source_device_id=f"{adapter_id}-brightness-backup",
                canonical_id="living_room.main_light",
                name="Main Light Backup Brightness",
                capabilities=[brightness_capability()],
            )
        )
    for index in range(extra_entities):
        entities.append(
            entity(
                entity_id=f"{adapter_id}.office_{index}",
                source_device_id=f"{adapter_id}-office-{index}",
                canonical_id=f"office.{adapter_id}-{index}",
                name=f"{adapter_id.title()} Office {index}",
                capabilities=[power_capability()],
                area_id="office",
            )
        )
    now = datetime.now(UTC)
    states = [
        {
            "entity_id": item["entity_id"],
            "capability": capability["name"],
            "value": False if capability["name"] == "power" else 20.0,
            "unit": capability.get("unit"),
            "available": True,
            "observed_at": now,
        }
        for item in entities
        for capability in item["capabilities"]
    ]
    return AdapterSnapshot(source_entities=entities, source_states=states)


class RecordingAdapter:
    """AdapterPort fixture with source-aware command recording."""

    def __init__(
        self,
        adapter_id: str,
        snapshot: AdapterSnapshot,
        *,
        fail_connect: bool = False,
        fail_discover: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.snapshot = snapshot
        self.fail_connect = fail_connect
        self.fail_discover = fail_discover
        self.connected = False
        self.available = True
        self.writes: list[tuple[str, Command]] = []
        self.execution_contexts: list[ExecutionContext | None] = []
        self.events: list[SourceEvent] = []

    async def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError(f"{self.adapter_id} fixture unavailable")
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def discover(self) -> AdapterSnapshot:
        if self.fail_discover:
            raise ConnectionError(f"{self.adapter_id} discovery unavailable")
        return deepcopy(self.snapshot)

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        wanted = {ref.external_id for ref in source_refs}
        now = datetime.now(UTC)
        return [
            StateSnapshot(
                device_id=state["entity_id"],
                capability=state["capability"],
                value=state.get("value"),
                unit=state.get("unit"),
                observed_at=state.get("observed_at", now),
                received_at=now,
                status=StateStatus.CURRENT if self.available else StateStatus.UNAVAILABLE,
                source_ref=SourceRef(adapter_id=self.adapter_id, external_id=state["entity_id"]),
            )
            for state in self.snapshot.source_states
            if state["entity_id"] in wanted
        ]

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        return await self.execute_source(command, command.device_id, execution_context)

    async def execute_source(
        self,
        command: Command,
        source_entity_id: str,
        execution_context: ExecutionContext | None = None,
    ) -> AdapterExecutionAck:
        self.execution_contexts.append(execution_context)
        if not self.connected or not self.available:
            return AdapterExecutionAck(accepted=False, message="Fixture source unavailable")
        self.writes.append((source_entity_id, command))
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=source_entity_id),
            message="Fixture source accepted",
        )

    def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        async def stream() -> AsyncIterator[SourceEvent]:
            while self.events:
                yield self.events.pop(0)

        return stream()

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            adapter_id=self.adapter_id,
            connected=self.connected and self.available,
            message=None if self.available else "Fixture source unavailable",
        )
