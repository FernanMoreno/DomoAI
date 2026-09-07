"""Pure projection of declared generic MQTT mappings."""

from __future__ import annotations

from domoai.adapters.mqtt.config import MqttAdapterMapping, MqttDeviceMapping
from domoai.domain.models import AdapterSnapshot


class MqttMapper:
    def to_snapshot(self, mapping: MqttAdapterMapping) -> AdapterSnapshot:
        return AdapterSnapshot(source_entities=[self.entity(device) for device in mapping.devices])

    def entity(self, device: MqttDeviceMapping) -> dict[str, object]:
        capabilities: list[dict[str, object]] = []
        for capability in device.capabilities:
            canonical: dict[str, object] = {
                "name": capability.name,
                "kind": capability.kind.value,
                "unit": capability.unit,
                "readable": True,
                "writable": capability.command_topic is not None,
                "minimum": capability.minimum,
                "maximum": capability.maximum,
                "commands": capability.commands
                or ([capability.name] if capability.command_topic is not None else []),
            }
            if capability.enum_values:
                canonical["enum_values"] = capability.enum_values
            capabilities.append(canonical)
        return {
            "entity_id": device.source_id,
            "device_id": device.source_id,
            "domain": device.type,
            "name": device.source_id,
            "area_id": "unassigned",
            "semantic_type": device.type,
            "capabilities": capabilities,
            "available": True,
        }
