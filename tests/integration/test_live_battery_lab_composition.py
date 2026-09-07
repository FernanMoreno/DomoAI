"""Opt-in composition checks for the shared virtual battery lab."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.adapters.home_assistant.config import load_home_assistant_mapping
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import canonical_device_id, load_mapping
from domoai.adapters.knx.transport import XknxTransport
from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.config import load_mapping as load_modbus_mapping
from domoai.adapters.modbus.transport import PyModbusTcpTransport
from domoai.domain.models import Command


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"missing {name}")
    return value


@pytest.mark.asyncio
async def test_live_battery_shared_state_across_http_modbus_and_home_assistant() -> None:
    if os.getenv("DOMOAI_LIVE_BATTERY_LAB_ENABLE") != "1":
        pytest.skip("set DOMOAI_LIVE_BATTERY_LAB_ENABLE=1 for the real battery lab")

    battery_url = os.getenv("DOMOAI_BATTERY_URL", "http://127.0.0.1:8090")
    modbus_host = os.getenv("DOMOAI_MODBUS_HOST", "127.0.0.1")
    modbus_port = int(os.getenv("DOMOAI_MODBUS_PORT", "1503"))
    modbus_mapping_path = Path(
        os.getenv("DOMOAI_MODBUS_CONFIG_PATH", "dev/lab/configs/modbus-battery.json")
    )
    ha_url = os.getenv("DOMOAI_HOME_ASSISTANT_URL")
    ha_token = os.getenv("DOMOAI_HOME_ASSISTANT_TOKEN")
    ha_mapping_path = Path(
        os.getenv(
            "DOMOAI_HOME_ASSISTANT_MAPPING_PATH",
            "dev/lab/configs/home-assistant-battery.json",
        )
    )
    if not ha_url or not ha_token:
        pytest.skip("Home Assistant credentials are required for the shared battery test")

    async with httpx.AsyncClient(timeout=5) as client:
        stopped = await client.post(
            f"{battery_url}/command",
            json={
                "command": "stop_battery",
                "idempotency_key": "live-battery-lab-stop-before",
            },
        )
        stopped.raise_for_status()

        charged = await client.post(
            f"{battery_url}/command",
            json={
                "command": "charge_battery",
                "value": 1.0,
                "idempotency_key": "live-battery-lab-http-charge",
            },
        )
        charged.raise_for_status()
        assert charged.json()["power_kw"] == pytest.approx(1.0)

    modbus_transport = PyModbusTcpTransport(modbus_host, port=modbus_port)
    modbus = ModbusAdapter(modbus_transport, load_modbus_mapping(modbus_mapping_path))
    await modbus.connect()
    try:
        snapshot = await modbus.discover()
        battery = next(
            entity for entity in snapshot.source_entities if entity["domain"] == "energy"
        )
        assert {capability["name"] for capability in battery["capabilities"]} >= {
            "battery.soc",
            "battery.power",
            "battery.capacity",
        }
        power = next(
            state for state in snapshot.source_states if state["capability"] == "battery.power"
        )
        assert power["value"] == pytest.approx(1.0, abs=0.01)
    finally:
        await modbus.disconnect()

    mapping = load_home_assistant_mapping(ha_mapping_path)
    ha_client = HomeAssistantClient(ha_url, ha_token)
    ha_provider = HomeAssistantProvider(
        ha_client,
        metric_mappings=mapping.metric_mappings,
        battery_capacity_bindings=mapping.battery_capacity_bindings,
        battery_dispatch_bindings=mapping.battery_dispatch_bindings,
    )
    ha = HomeAssistantProviderAdapter(ha_provider)
    await ha.connect()
    try:
        ha_snapshot = await ha.discover()
        ha_provider.validate_battery_dispatch_routes(ha_snapshot)
        command_entity = next(
            entity
            for entity in ha_snapshot.source_entities
            if any(
                "charge_battery" in capability.get("commands", [])
                for capability in entity.get("capabilities", [])
            )
        )
        # The canonical ID is stable for this discovered source device and is
        # intentionally derived by the adapter, not guessed from the mapping.
        canonical = ha._canonical_by_source[command_entity["entity_id"]]
        result = await ha.execute(
            Command(
                id="live-battery-lab-ha-stop",
                device_id=canonical,
                command="stop_battery",
                idempotency_key="live-battery-lab-ha-stop",
            )
        )
        assert result.accepted is True
        await asyncio.sleep(0.5)
        states = {
            item["entity_id"]: item["state"]
            for item in await ha_client.fetch_states()
            if "virtual_battery" in item["entity_id"]
        }
        power_entity = mapping.battery_dispatch_bindings["lab-battery"].power_feedback_entity_id
        assert float(states[power_entity]) == pytest.approx(0.0, abs=0.01)
    finally:
        await ha.disconnect()


@pytest.mark.asyncio
async def test_live_battery_knx_virtual_connection_and_optional_readback() -> None:
    if os.getenv("DOMOAI_LIVE_BATTERY_KNX_ENABLE") != "1":
        pytest.skip("set DOMOAI_LIVE_BATTERY_KNX_ENABLE=1 for the KNX battery facade")
    host = _required("DOMOAI_KNX_GATEWAY_HOST")
    mapping_path = Path(
        os.getenv("DOMOAI_KNX_CONFIG_PATH", "dev/lab/configs/knx-battery-virtual.json")
    )
    mapping = load_mapping(mapping_path)
    group_dpts = {
        address: binding.dpt
        for entity in mapping.entities
        for binding in entity.capabilities
        for address in (binding.state_group_address, binding.command_group_address)
        if address is not None
    }
    transport = XknxTransport(
        host,
        gateway_port=int(os.getenv("DOMOAI_KNX_GATEWAY_PORT", "3672")),
        route_back=os.getenv("DOMOAI_KNX_ROUTE_BACK", "0").strip().lower()
        in {"1", "true", "yes", "on"},
        group_dpts=group_dpts,
    )
    adapter = KnxAdapter(transport, mapping)
    await adapter.connect()
    command_sent = False
    battery_device_id: str | None = None
    try:
        snapshot = await adapter.discover()
        assert await transport.health() is True
        if os.getenv("DOMOAI_LIVE_BATTERY_KNX_READBACK_REQUIRED") == "1":
            state_by_capability = {
                state["capability"]: state["value"] for state in snapshot.source_states
            }
            assert set(state_by_capability) >= {
                "battery.power",
                "battery.soc",
                "battery.capacity",
            }, "ETS groups have no complete battery readback"
            battery_url = os.getenv("DOMOAI_BATTERY_URL", "http://127.0.0.1:8090")
            async with httpx.AsyncClient(timeout=5) as client:
                battery_state = await client.get(f"{battery_url}/state")
                battery_state.raise_for_status()
            assert state_by_capability["battery.power"] == pytest.approx(
                battery_state.json()["power_kw"], abs=0.01
            )

        if os.getenv("DOMOAI_LIVE_BATTERY_KNX_COMMAND_REQUIRED") == "1":
            battery_entity = next(
                entity
                for entity in snapshot.source_entities
                if entity["domain"] == "energy"
            )
            battery_config = next(
                entity
                for entity in mapping.entities
                if entity.entity_id == battery_entity["entity_id"]
            )
            battery_device_id = canonical_device_id(battery_config)
            result = await adapter.execute(
                Command(
                    id="live-battery-knx-charge",
                    device_id=battery_device_id,
                    command="charge_battery",
                    value=0.5,
                    unit="kW",
                    idempotency_key="live-battery-knx-charge",
                )
            )
            assert result.accepted is True
            command_sent = True
            async with httpx.AsyncClient(timeout=5) as client:
                for _ in range(30):
                    response = await client.get(f"{battery_url}/state")
                    response.raise_for_status()
                    if response.json()["power_kw"] == pytest.approx(0.5, abs=0.01):
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("KNX command did not reach the battery facade")

            refreshed = await adapter.discover()
            refreshed_power = next(
                state["value"]
                for state in refreshed.source_states
                if state["capability"] == "battery.power"
            )
            assert refreshed_power == pytest.approx(0.5, abs=0.01)
    finally:
        if command_sent and battery_device_id is not None:
            try:
                await adapter.execute(
                    Command(
                        id="live-battery-knx-stop",
                        device_id=battery_device_id,
                        command="stop_battery",
                        idempotency_key="live-battery-knx-stop",
                    )
                )
            except Exception:
                pass
        await adapter.disconnect()
