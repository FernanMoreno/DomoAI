"""Deterministic Matter Server WebSocket messages for adapter tests."""

from __future__ import annotations

from typing import Any

from domoai.adapters.matter.transport import MatterTransportMessage


def server_info(*, schema_version: int = 13, minimum_schema_version: int = 11) -> dict[str, Any]:
    return {
        "fabric_id": 1234,
        "compressed_fabric_id": 5678,
        "schema_version": schema_version,
        "min_supported_schema_version": minimum_schema_version,
        "sdk_version": "fixture-matter-server",
        "wifi_credentials_set": False,
        "thread_credentials_set": False,
        "bluetooth_enabled": False,
    }


def node_snapshot(
    node_id: int,
    *,
    endpoint_id: int = 1,
    profile: str = "dimmable_light",
    available: bool = True,
    on: bool = True,
    level: int = 127,
) -> dict[str, Any]:
    device_types = {
        "onoff_light": 0x0100,
        "dimmable_light": 0x0101,
        "onoff_plug": 0x010A,
        "dimmable_plug": 0x010B,
        "unknown": 0x9999,
    }
    device_type = device_types[profile]
    attributes: dict[str, Any] = {
        "0/40/1": "Fixture Matter Vendor",
        "0/40/3": "Fixture Matter Product",
        "0/40/5": f"Matter Fixture {node_id}",
        f"{endpoint_id}/29/0": [{"deviceType": device_type, "revision": 4}],
        f"{endpoint_id}/6/0": on,
    }
    if profile in {"dimmable_light", "dimmable_plug"}:
        attributes[f"{endpoint_id}/8/0"] = level
    return {
        "node_id": node_id,
        "date_commissioned": "2026-08-15T00:00:00.000Z",
        "last_interview": "2026-08-15T00:00:01.000Z",
        "interview_version": 1,
        "available": available,
        "is_bridge": False,
        "attributes": attributes,
    }


def sensor_node(node_id: int, *, endpoint_id: int = 1, available: bool = True) -> dict[str, Any]:
    node = node_snapshot(node_id, endpoint_id=endpoint_id, profile="unknown", available=available)
    attributes = node["attributes"]
    attributes.pop(f"{endpoint_id}/6/0")
    attributes.update(
        {
            f"{endpoint_id}/1026/0": 2150,
            f"{endpoint_id}/1029/0": 4250,
            f"{endpoint_id}/1030/0": 1,
        }
    )
    return node


def node_snapshots(count: int = 3) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for index in range(count):
        profile = "dimmable_light" if index % 2 == 0 else "onoff_plug"
        nodes.append(node_snapshot(1001 + index, profile=profile))
    return nodes


def event_message(event: str, data: Any) -> MatterTransportMessage:
    return MatterTransportMessage(payload={"event": event, "data": data})


def malformed_message() -> MatterTransportMessage:
    return MatterTransportMessage(payload={"event": "attribute_updated", "data": ["bad"]})
