"""Map Matter Server node snapshots into the canonical adapter contracts."""

from __future__ import annotations

import re
from typing import Any

from domoai.domain.models import AdapterSnapshot, Capability, CapabilityKind, DeviceType

DESCRIPTOR_CLUSTER = 29
BASIC_INFORMATION_CLUSTER = 40
ON_OFF_CLUSTER = 6
LEVEL_CONTROL_CLUSTER = 8
TEMPERATURE_CLUSTER = 1026
HUMIDITY_CLUSTER = 1029
OCCUPANCY_CLUSTER = 1030

ON_OFF_LIGHT = 0x0100
DIMMABLE_LIGHT = 0x0101
ON_OFF_PLUG = 0x010A
DIMMABLE_PLUG = 0x010B


class MatterMapper:
    """Pure mapper for the bounded Matter Server v1 profile."""

    def to_snapshot(self, nodes: list[dict[str, Any]]) -> AdapterSnapshot:
        source_entities: list[dict[str, Any]] = []
        source_states: list[dict[str, Any]] = []
        unsupported_sources: list[dict[str, Any]] = []
        for node in nodes:
            entities, unsupported = self._map_node(node)
            source_entities.extend(entities)
            unsupported_sources.extend(unsupported)
            states, _diagnostics = self.map_states(node)
            source_states.extend(states)
        return AdapterSnapshot(
            source_entities=source_entities,
            source_states=source_states,
            unsupported_sources=unsupported_sources,
        )

    def map_states(
        self, node: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        node_id = self._node_id(node)
        available = bool(node.get("available", False))
        attributes = self._attributes(node)
        states: list[dict[str, Any]] = []
        diagnostics: list[str] = []
        for endpoint_id in self._endpoint_ids(attributes):
            entity_id = self.source_entity_id(node_id, endpoint_id)
            for cluster, attribute, capability, unit in (
                (ON_OFF_CLUSTER, 0, "power", None),
                (LEVEL_CONTROL_CLUSTER, 0, "brightness", "%"),
                (TEMPERATURE_CLUSTER, 0, "temperature", "°C"),
                (HUMIDITY_CLUSTER, 0, "humidity", "%"),
                (OCCUPANCY_CLUSTER, 0, "occupancy", None),
            ):
                path = self.path(endpoint_id, cluster, attribute)
                if path not in attributes:
                    continue
                value, reason = self._convert_state(capability, attributes[path])
                if reason is not None:
                    diagnostics.append(f"{capability} {reason}")
                    continue
                states.append(
                    {
                        "entity_id": entity_id,
                        "capability": capability,
                        "value": value,
                        "unit": unit,
                        "available": available,
                    }
                )
        return states, diagnostics

    @staticmethod
    def source_entity_id(node_id: int, endpoint_id: int) -> str:
        return f"node:{node_id}/endpoint:{endpoint_id}"

    @staticmethod
    def path(endpoint_id: int, cluster_id: int, attribute_id: int) -> str:
        return f"{endpoint_id}/{cluster_id}/{attribute_id}"

    def _map_node(
        self, node: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        node_id = self._node_id(node)
        available = bool(node.get("available", False))
        attributes = self._attributes(node)
        metadata = self._metadata(attributes)
        entities: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        for endpoint_id in self._endpoint_ids(attributes):
            device_types = self._device_types(
                attributes.get(self.path(endpoint_id, DESCRIPTOR_CLUSTER, 0))
            )
            semantic_type, domain, writable_profile = self._profile(device_types)
            if device_types.intersection(
                {ON_OFF_LIGHT, DIMMABLE_LIGHT, ON_OFF_PLUG, DIMMABLE_PLUG}
            ) and self.path(endpoint_id, ON_OFF_CLUSTER, 0) not in attributes:
                semantic_type = DeviceType.UNSUPPORTED
                domain, writable_profile = "unsupported", False
            if semantic_type is DeviceType.UNSUPPORTED and self._has_sensor_capability(
                attributes, endpoint_id
            ):
                semantic_type, domain = DeviceType.SENSOR, "sensor"
            capabilities = self._capabilities(
                attributes,
                endpoint_id,
                writable_profile=writable_profile,
                semantic_type=semantic_type,
            )
            entity_id = self.source_entity_id(node_id, endpoint_id)
            if semantic_type is DeviceType.UNSUPPORTED:
                unsupported.append(
                    {
                        "entity_id": entity_id,
                        "node_id": node_id,
                        "endpoint_id": endpoint_id,
                        "reason": "unsupported Matter device type",
                        "device_types": sorted(device_types),
                    }
                )
                continue
            name = metadata["name"]
            if endpoint_id != 1 or len(self._endpoint_ids(attributes)) > 1:
                name = f"{name} endpoint {endpoint_id}"
            entities.append(
                {
                    "entity_id": entity_id,
                    "device_id": entity_id,
                    "domain": domain,
                    "name": name,
                    "area_id": "unassigned",
                    "manufacturer": metadata["manufacturer"],
                    "model": metadata["model"],
                    "semantic_type": semantic_type.value,
                    "capabilities": [capability.model_dump() for capability in capabilities],
                    "available": available,
                }
            )
        return entities, unsupported

    def _capabilities(
        self,
        attributes: dict[str, Any],
        endpoint_id: int,
        *,
        writable_profile: bool,
        semantic_type: DeviceType,
    ) -> list[Capability]:
        capabilities: list[Capability] = []
        on_off_path = self.path(endpoint_id, ON_OFF_CLUSTER, 0)
        level_path = self.path(endpoint_id, LEVEL_CONTROL_CLUSTER, 0)
        if on_off_path in attributes:
            capabilities.append(
                Capability(
                    name="power",
                    kind=CapabilityKind.BOOLEAN,
                    readable=True,
                    writable=writable_profile,
                    commands=["turn_on", "turn_off", "toggle"] if writable_profile else [],
                )
            )
        if level_path in attributes:
            dimmable = writable_profile and semantic_type in {DeviceType.LIGHT, DeviceType.SWITCH}
            capabilities.append(
                Capability(
                    name="brightness",
                    kind=CapabilityKind.INTEGER,
                    unit="%",
                    readable=True,
                    writable=dimmable,
                    minimum=0,
                    maximum=100,
                    commands=["set_brightness"] if dimmable else [],
                )
            )
        if self.path(endpoint_id, TEMPERATURE_CLUSTER, 0) in attributes:
            capabilities.append(
                Capability(
                    name="temperature",
                    kind=CapabilityKind.NUMBER,
                    unit="°C",
                    readable=True,
                    writable=False,
                )
            )
        if self.path(endpoint_id, HUMIDITY_CLUSTER, 0) in attributes:
            capabilities.append(
                Capability(
                    name="humidity",
                    kind=CapabilityKind.NUMBER,
                    unit="%",
                    readable=True,
                    writable=False,
                    minimum=0,
                    maximum=100,
                )
            )
        if self.path(endpoint_id, OCCUPANCY_CLUSTER, 0) in attributes:
            capabilities.append(
                Capability(
                    name="occupancy",
                    kind=CapabilityKind.BOOLEAN,
                    readable=True,
                    writable=False,
                )
            )
        return capabilities

    @staticmethod
    def _profile(device_types: set[int]) -> tuple[DeviceType, str, bool]:
        if DIMMABLE_LIGHT in device_types:
            return DeviceType.LIGHT, "light", True
        if ON_OFF_LIGHT in device_types:
            return DeviceType.LIGHT, "light", True
        if DIMMABLE_PLUG in device_types:
            return DeviceType.SWITCH, "switch", True
        if ON_OFF_PLUG in device_types:
            return DeviceType.SWITCH, "switch", True
        if device_types:
            return DeviceType.UNSUPPORTED, "unsupported", False
        return DeviceType.SENSOR, "sensor", False

    @staticmethod
    def _convert_state(capability: str, value: Any) -> tuple[object | None, str | None]:
        if capability == "power":
            if isinstance(value, bool):
                return value, None
            return None, "value must be boolean"
        if capability == "brightness":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 254:
                return None, "must be an integer between 0 and 254"
            return round(value * 100 / 254), None
        if capability == "temperature":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, "value must be numeric"
            return value / 100, None
        if capability == "humidity":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, "value must be numeric"
            converted = value / 100
            if not 0 <= converted <= 100:
                return None, "value must be between 0 and 100 percent"
            return converted, None
        if capability == "occupancy":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None, "value must be a non-negative occupancy bitmap"
            return bool(value & 1), None
        return None, "unsupported capability"

    @staticmethod
    def _node_id(node: dict[str, Any]) -> int:
        value = node.get("node_id")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Matter node_id must be a non-negative integer")
        return value

    @staticmethod
    def _attributes(node: dict[str, Any]) -> dict[str, Any]:
        attributes = node.get("attributes")
        if not isinstance(attributes, dict):
            raise ValueError("Matter node attributes must be an object")
        return {str(path): value for path, value in attributes.items() if isinstance(path, str)}

    @staticmethod
    def _endpoint_ids(attributes: dict[str, Any]) -> list[int]:
        endpoints: set[int] = set()
        for path in attributes:
            parts = path.split("/")
            if len(parts) != 3:
                continue
            try:
                endpoint_id = int(parts[0])
                if endpoint_id != 0:
                    endpoints.add(endpoint_id)
            except ValueError:
                continue
        return sorted(endpoints)

    @classmethod
    def _has_sensor_capability(cls, attributes: dict[str, Any], endpoint_id: int) -> bool:
        return any(
            cls.path(endpoint_id, cluster_id, 0) in attributes
            for cluster_id in (TEMPERATURE_CLUSTER, HUMIDITY_CLUSTER, OCCUPANCY_CLUSTER)
        )

    @staticmethod
    def _device_types(value: Any) -> set[int]:
        if not isinstance(value, list):
            return set()
        result: set[int] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            # Python Matter Server uses named fields; matter.js serializes the
            # same struct with numeric fields (0=device type, 1=revision).
            raw_type = item.get(
                "deviceType",
                item.get("device_type", item.get("0")),
            )
            if isinstance(raw_type, int) and not isinstance(raw_type, bool):
                result.add(raw_type)
        return result

    @staticmethod
    def _metadata(attributes: dict[str, Any]) -> dict[str, str | None]:
        def text(path: str) -> str | None:
            value = attributes.get(path)
            return str(value) if isinstance(value, (str, int)) else None

        return {
            "name": text("0/40/5") or "Matter device",
            "manufacturer": text("0/40/1"),
            "model": text("0/40/3") or text("0/40/4"),
        }


def canonical_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return f"unassigned.{normalized or 'device'}"
