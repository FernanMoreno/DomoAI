"""Translate Home Assistant-shaped entities into adapter snapshots."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from domoai.domain.models import (
    AdapterSnapshot,
    Capability,
    CapabilityKind,
    DeviceType,
)


class HomeAssistantMapper:
    adapter_id = "home_assistant"

    def to_snapshot(self, entities: list[dict[str, Any]]) -> AdapterSnapshot:
        source_entities: list[dict[str, Any]] = []
        source_states: list[dict[str, Any]] = []
        areas: dict[str, dict[str, Any]] = {}

        for raw_entity in entities:
            entity = self._normalize_entity(raw_entity)
            capabilities = self._capabilities(entity)
            area_id = entity.get("area_id") or "unassigned"
            areas.setdefault(area_id, {"id": area_id, "name": area_id.replace("_", " ").title()})
            source_entities.append(
                {
                    "entity_id": entity["entity_id"],
                    "device_id": entity.get("device_id") or entity["entity_id"],
                    "domain": entity["domain"],
                    "name": entity.get("name") or entity["entity_id"],
                    "area_id": area_id,
                    "manufacturer": entity.get("manufacturer"),
                    "model": entity.get("model"),
                    "semantic_type": self._device_type(entity).value,
                    "capabilities": [capability.model_dump() for capability in capabilities],
                    "available": entity.get("available", True),
                    **self._source_metadata(entity),
                }
            )
            source_states.extend(self._states(entity, capabilities))

        return AdapterSnapshot(
            source_entities=source_entities,
            source_states=source_states,
            areas=list(areas.values()),
        )

    def _normalize_entity(self, raw_entity: dict[str, Any]) -> dict[str, Any]:
        entity = deepcopy(raw_entity)
        entity_id = str(entity["entity_id"])
        domain = entity.get("domain") or entity_id.split(".", 1)[0]
        entity["domain"] = domain
        if not isinstance(entity.get("state"), dict):
            raw_state = entity.get("state")
            entity["state"] = {"power": raw_state}
        attributes = entity.get("attributes", {})
        if isinstance(attributes, dict):
            entity["attributes"] = attributes
        return entity

    @staticmethod
    def _source_metadata(entity: dict[str, Any]) -> dict[str, Any]:
        """Preserve HA device-registry identity without making it the runtime model.

        HA entities may expose several entities for one physical device.  The
        runtime therefore keeps the HA device id as the source-device key and
        copies registry identifiers/connections only as optional identity
        evidence.  A caller may provide ``canonical_id`` when it has an
        explicit cross-adapter link; the runtime never infers one from a
        fuzzy name match.
        """

        metadata: dict[str, Any] = {}
        identity_keys = entity.get("identity_keys", entity.get("identifiers"))
        if identity_keys is not None:
            metadata["identity_keys"] = _registry_values(identity_keys)
        connections = entity.get("connections")
        if connections is not None:
            metadata["connections"] = _registry_values(connections)
        canonical_id = entity.get("canonical_id")
        if canonical_id is not None:
            metadata["canonical_id"] = canonical_id
        parent = entity.get("parent_source_device_id", entity.get("parent_device_id"))
        via_device = entity.get("via_device")
        if parent is None and via_device is not None:
            parent = _registry_value(via_device)
        if parent is not None:
            metadata["parent_source_device_id"] = _registry_value(parent)
        local_canonical_id = entity.get("local_canonical_id")
        if local_canonical_id is not None:
            metadata["local_canonical_id"] = local_canonical_id
        return metadata

    def _device_type(self, entity: dict[str, Any]) -> DeviceType:
        domain = entity["domain"]
        if domain == "light":
            return DeviceType.LIGHT
        if domain == "switch":
            return DeviceType.SWITCH
        if domain == "cover":
            return DeviceType.COVER
        if domain == "climate":
            return DeviceType.CLIMATE
        if domain == "sensor":
            measurement = entity.get("attributes", {}).get("measurement")
            return DeviceType.ENERGY if measurement in {"power", "energy"} else DeviceType.SENSOR
        return DeviceType.UNSUPPORTED

    def _capabilities(self, entity: dict[str, Any]) -> list[Capability]:
        domain = entity["domain"]
        attributes = entity.get("attributes", {})
        supported = set(entity.get("supported_features", []))
        if domain == "light":
            capabilities: list[Capability] = []
            if "power" in entity.get("state", {}):
                capabilities.append(
                    Capability(
                        name="power",
                        kind=CapabilityKind.BOOLEAN,
                        readable=True,
                        writable=True,
                        commands=["turn_on", "turn_off", "toggle"],
                    )
                )
            if "brightness" in supported:
                capabilities.append(
                    Capability(
                        name="brightness",
                        kind=CapabilityKind.INTEGER,
                        unit="%",
                        readable=True,
                        writable=True,
                        minimum=attributes.get("brightness_min", 0),
                        maximum=attributes.get("brightness_max", 100),
                        commands=["set_brightness"],
                    )
                )
            return capabilities
        if domain == "switch":
            return [
                Capability(
                    name="power",
                    kind=CapabilityKind.BOOLEAN,
                    readable=True,
                    writable=True,
                    commands=["turn_on", "turn_off", "toggle"],
                )
            ]
        if domain == "cover":
            return [
                Capability(
                    name="position",
                    kind=CapabilityKind.INTEGER,
                    unit="%",
                    readable=True,
                    writable=True,
                    minimum=attributes.get("position_min", 0),
                    maximum=attributes.get("position_max", 100),
                    commands=["set_position", "open", "close", "stop"],
                )
            ]
        if domain == "climate":
            unit = attributes.get("unit", "°C")
            return [
                Capability(
                    name="temperature",
                    kind=CapabilityKind.NUMBER,
                    unit=unit,
                    readable=True,
                    writable=False,
                ),
                Capability(
                    name="target_temperature",
                    kind=CapabilityKind.NUMBER,
                    unit=unit,
                    readable=True,
                    writable=True,
                    minimum=attributes.get("temperature_min", 16),
                    maximum=attributes.get("temperature_max", 27),
                    commands=["set_temperature"],
                ),
            ]
        if domain == "sensor":
            measurement = attributes.get("measurement", "value")
            return [
                Capability(
                    name=measurement,
                    kind=CapabilityKind.NUMBER,
                    unit=attributes.get("unit"),
                    readable=True,
                    writable=False,
                )
            ]
        return []

    def _states(
        self, entity: dict[str, Any], capabilities: list[Capability]
    ) -> list[dict[str, Any]]:
        state = entity.get("state", {})
        return [
            {
                "entity_id": entity["entity_id"],
                "capability": capability.name,
                "value": state.get(capability.name),
                "unit": capability.unit,
                "available": entity.get("available", True),
                "observed_at": entity.get("last_updated") or entity.get("last_changed"),
                "received_at": entity.get("received_at"),
            }
            for capability in capabilities
            if capability.name in state
        ]


def _registry_values(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return [_registry_value(item) for item in value]


def _registry_value(value: object) -> str:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{value[0]}:{value[1]}"
    return str(value)
