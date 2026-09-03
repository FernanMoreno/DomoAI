"""Docker water meter projection: HTTP, MQTT and Modbus TCP over one model.

Purely read-only (spec 163 research.md Decision 4): unlike the battery/EV
lab servers, there is no `/command` endpoint and no writable Modbus
register -- `/flow` is a lab-harness control for driving the simulation,
not a projection of any canonical device command.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from pymodbus.datastore import ModbusServerContext, ModbusSimulatorContext
from pymodbus.server import StartAsyncTcpServer

from domoai.lab.water_consumption_simulator import (
    WaterConsumptionSimulationProfile,
    WaterConsumptionSimulator,
)


class WaterMeterLabServer:
    def __init__(self, profile_path: Path) -> None:
        profile = WaterConsumptionSimulationProfile.from_dict(
            json.loads(profile_path.read_text(encoding="utf-8"))
        )
        self.simulator = WaterConsumptionSimulator(profile)
        self.mqtt_topic = os.getenv("WATER_METER_MQTT_TOPIC", "domoai/water-meter")
        self._last_tick = time.monotonic()
        self._mqtt_client: Any = None

    def tick(self) -> dict[str, Any]:
        now = time.monotonic()
        elapsed = min(max(now - self._last_tick, 0.0), 10.0)
        self._last_tick = now
        if elapsed:
            self.simulator.tick(elapsed)
        return self.simulator.snapshot().as_dict()

    def publish(self, payload: dict[str, Any], client: Any | None = None) -> None:
        mqtt_client = client if client is not None else self._mqtt_client
        if mqtt_client is not None:
            mqtt_client.publish(f"{self.mqtt_topic}/state", json.dumps(payload), retain=True)

    def publish_discovery(self, client: Any | None = None) -> None:
        mqtt_client = client if client is not None else self._mqtt_client
        if mqtt_client is None:
            return
        device = {
            "identifiers": [self.simulator.profile.device_id],
            "name": "DomoAI Virtual Water Meter",
            "manufacturer": "DomoAI Lab",
            "model": "Deterministic Water Meter",
        }
        common = {
            "state_topic": f"{self.mqtt_topic}/state",
            "availability_topic": f"{self.mqtt_topic}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        sensors = {
            "flow_rate": {
                "name": "Virtual Water Flow Rate",
                "value_template": "{{ value_json.flow_rate_lpm }}",
                "unit_of_measurement": "L/min",
            },
            "total_volume": {
                "name": "Virtual Water Total Volume",
                "value_template": "{{ value_json.total_volume_l }}",
                "unit_of_measurement": "L",
                "device_class": "water",
                "state_class": "total_increasing",
            },
        }
        for suffix, payload in sensors.items():
            mqtt_client.publish(
                f"homeassistant/sensor/domoai_virtual_water_meter_{suffix}/config",
                json.dumps(
                    {
                        **common,
                        **payload,
                        "unique_id": f"domoai_virtual_water_meter_{suffix}",
                    }
                ),
                retain=True,
            )
        mqtt_client.publish(f"{self.mqtt_topic}/availability", "online", retain=True)

    def _publish_current_state(self, client: Any) -> None:
        self.publish(self.tick(), client)

    def _handle_mqtt_connect(
        self,
        client: Any,
        reason_code: Any,
        connected_loop: asyncio.AbstractEventLoop,
        connected: asyncio.Event,
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            return
        client.subscribe(f"{self.mqtt_topic}/flow/set")
        # Discovery is retained by MQTT and must be renewed after every broker
        # reconnect. The callback may run before self._mqtt_client is assigned,
        # so pass the callback client explicitly.
        self.publish_discovery(client)
        connected_loop.call_soon_threadsafe(self._publish_current_state, client)
        connected_loop.call_soon_threadsafe(connected.set)

    async def start_mqtt(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as error:  # pragma: no cover - image dependency guard
            raise RuntimeError("paho-mqtt is required for the water meter lab") from error

        host = os.getenv("MQTT_HOST", "mqtt")
        port = int(os.getenv("MQTT_PORT", "1883"))
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="domoai-lab-water-meter")

        def on_message(_client: Any, _userdata: Any, message: Any) -> None:
            if message.topic != f"{self.mqtt_topic}/flow/set":
                return
            try:
                result = self.simulator.set_flow_rate(float(message.payload.decode("utf-8")))
                self.publish(result.as_dict())
            except (ConnectionError, ValueError) as error:
                self.publish(
                    {
                        "schema_version": "v1",
                        "available": False,
                        "fault": "rejected",
                        "error": str(error)[:200],
                    }
                )

        client.on_message = on_message
        connected_loop = asyncio.get_running_loop()
        connected = asyncio.Event()

        def on_connect(
            _client: Any,
            _userdata: Any,
            _flags: Any,
            reason_code: Any,
            _properties: Any = None,
        ) -> None:
            self._handle_mqtt_connect(_client, reason_code, connected_loop, connected)

        client.on_connect = on_connect
        client.connect_async(host, port, keepalive=30)
        client.loop_start()
        self._mqtt_client = client
        await asyncio.wait_for(connected.wait(), timeout=10)

    async def stop_mqtt(self) -> None:
        if self._mqtt_client is not None:
            self._mqtt_client.publish(f"{self.mqtt_topic}/availability", "offline", retain=True)
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None

    async def health(self, _request: web.Request) -> web.Response:
        state = self.tick()
        return web.json_response({"status": "ok", "lab_simulation": True, **state})

    async def state(self, _request: web.Request) -> web.Response:
        return web.json_response(self.tick())

    async def flow(self, request: web.Request) -> web.Response:
        # Lab-harness control, not a canonical device command (Decision 4) --
        # a real water meter has no writable capability at all.
        payload = await request.json()
        result = self.simulator.set_flow_rate(float(payload["flow_rate_lpm"]))
        serialized = result.as_dict()
        self.publish(serialized)
        return web.json_response(serialized)

    async def fault(self, request: web.Request) -> web.Response:
        payload = await request.json()
        result = self.simulator.set_fault(payload.get("fault"))
        serialized = result.as_dict()
        self.publish(serialized)
        return web.json_response(serialized)

    def modbus_context(self) -> ModbusServerContext:
        server = self

        def update_registers(registers: list[Any], _inx: int, _cell: Any, **_params: Any) -> None:
            now = time.monotonic()
            elapsed = min(max(now - server._last_tick, 0.0), 10.0)
            server._last_tick = now
            if elapsed:
                server.simulator.tick(elapsed)
            state = server.simulator.snapshot()
            for address, value in (
                (0, state.flow_rate_lpm),
                (2, state.total_volume_l),
            ):
                encoded = struct.pack(">f", float(value))
                registers[address].value = int.from_bytes(encoded[:2], "big")
                registers[address + 1].value = int.from_bytes(encoded[2:], "big")

        config = {
            "setup": {
                "di size": 0,
                "co size": 0,
                "ir size": 8,
                "hr size": 8,
                "shared blocks": True,
                "type exception": False,
                "defaults": {
                    "value": {
                        "bits": 0,
                        "uint16": 0,
                        "uint32": 0,
                        "float32": 0.0,
                        "string": " ",
                    },
                    "action": {
                        "bits": None,
                        "uint16": None,
                        "uint32": None,
                        "float32": None,
                        "string": None,
                    },
                },
            },
            "invalid": [],
            "write": [],
            "bits": [],
            "uint16": [],
            "uint32": [],
            "float32": [
                {"addr": [0, 1], "value": 0.0, "action": "water_state"},
                {"addr": [2, 3], "value": 0.0, "action": "water_state"},
            ],
            "string": [],
            "repeat": [],
        }
        return ModbusServerContext(
            ModbusSimulatorContext(config, {"water_state": update_registers})
        )


async def main() -> None:
    service = WaterMeterLabServer(Path(os.getenv("WATER_METER_PROFILE", "/app/profile.json")))
    app = web.Application()
    app.add_routes(
        [
            web.get("/health", service.health),
            web.get("/state", service.state),
            web.post("/flow", service.flow),
            web.post("/fault", service.fault),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("WATER_METER_HTTP_PORT", "8092")))
    await site.start()
    await service.start_mqtt()
    modbus_task = asyncio.create_task(
        StartAsyncTcpServer(
            service.modbus_context(),
            address=("0.0.0.0", int(os.getenv("WATER_METER_MODBUS_PORT", "1502"))),
        )
    )
    try:
        await asyncio.Event().wait()
    finally:
        modbus_task.cancel()
        await service.stop_mqtt()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
