"""Docker battery projection: HTTP, MQTT and Modbus TCP over one model."""

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

from domoai.lab.battery_simulator import BatterySimulationProfile, BatterySimulator


class BatteryLabServer:
    def __init__(self, profile_path: Path) -> None:
        profile = BatterySimulationProfile.from_dict(
            json.loads(profile_path.read_text(encoding="utf-8"))
        )
        self.simulator = BatterySimulator(profile)
        self.mqtt_topic = os.getenv("BATTERY_MQTT_TOPIC", "domoai/battery")
        self._last_command = 0.0
        self._last_tick = time.monotonic()
        self._mqtt_client: Any = None

    def tick(self) -> dict[str, Any]:
        now = time.monotonic()
        elapsed = min(max(now - self._last_tick, 0.0), 10.0)
        self._last_tick = now
        if elapsed:
            self.simulator.tick(elapsed)
        return self.simulator.snapshot().as_dict()

    def apply_power_setpoint(self, value: float) -> dict[str, Any]:
        if value > 0:
            command = "charge_battery"
            command_value = value
        elif value < 0:
            command = "discharge_battery"
            command_value = abs(value)
        else:
            command = "stop_battery"
            command_value = None
        result = self.simulator.command(
            command,
            value=command_value,
            idempotency_key=f"mqtt-power-{self.simulator.snapshot().revision + 1}-{value}",
        )
        return result.as_dict()

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
            "name": "DomoAI Virtual Battery",
            "manufacturer": "DomoAI Lab",
            "model": "Deterministic Battery",
        }
        common = {
            "state_topic": f"{self.mqtt_topic}/state",
            "availability_topic": f"{self.mqtt_topic}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        sensors = {
            "soc": {
                "name": "Virtual Battery SOC",
                "value_template": (
                    "{{ (value_json.soc_kwh / value_json.capacity_kwh * 100) | "
                    "round(3) }}"
                ),
                "unit_of_measurement": "%",
            },
            "power": {
                "name": "Virtual Battery Power",
                "value_template": "{{ value_json.power_kw }}",
                "unit_of_measurement": "kW",
            },
            "capacity": {
                "name": "Virtual Battery Capacity",
                "value_template": "{{ value_json.capacity_kwh }}",
                "unit_of_measurement": "kWh",
                "device_class": "energy_storage",
            },
        }
        for suffix, payload in sensors.items():
            mqtt_client.publish(
                f"homeassistant/sensor/domoai_virtual_battery_{suffix}/config",
                json.dumps(
                    {
                        **common,
                        **payload,
                        "unique_id": f"domoai_virtual_battery_{suffix}",
                    }
                ),
                retain=True,
            )
        mqtt_client.publish(
            "homeassistant/number/domoai_virtual_battery_power_command/config",
            json.dumps(
                {
                    **common,
                    "name": "Virtual Battery Power Command",
                    "unique_id": "domoai_virtual_battery_power_command",
                    "command_topic": f"{self.mqtt_topic}/power/set",
                    "value_template": "{{ value_json.power_kw }}",
                    "min": -self.simulator.profile.max_discharge_kw,
                    "max": self.simulator.profile.max_charge_kw,
                    "step": 0.1,
                    "unit_of_measurement": "kW",
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
        client.subscribe(f"{self.mqtt_topic}/command")
        client.subscribe(f"{self.mqtt_topic}/power/set")
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
            raise RuntimeError("paho-mqtt is required for the battery lab") from error

        host = os.getenv("MQTT_HOST", "mqtt")
        port = int(os.getenv("MQTT_PORT", "1883"))
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="domoai-lab-battery")

        def on_message(_client: Any, _userdata: Any, message: Any) -> None:
            raw = message.payload.decode("utf-8")
            try:
                if message.topic == f"{self.mqtt_topic}/command":
                    payload = json.loads(raw)
                    result = self.simulator.command(
                        str(payload["command"]),
                        value=payload.get("value"),
                        idempotency_key=str(payload["idempotency_key"]),
                    )
                elif message.topic == f"{self.mqtt_topic}/power/set":
                    result = self.apply_power_setpoint(float(raw))
                else:
                    return
                self.publish(result.as_dict() if hasattr(result, "as_dict") else result)
            except (
                ConnectionError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
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

    async def command(self, request: web.Request) -> web.Response:
        payload = await request.json()
        result = self.simulator.command(
            str(payload["command"]),
            value=payload.get("value"),
            idempotency_key=str(payload["idempotency_key"]),
        )
        serialized = result.as_dict()
        self.publish(serialized)
        return web.json_response(serialized)

    async def tick_endpoint(self, request: web.Request) -> web.Response:
        payload = await request.json() if request.can_read_body else {}
        result = self.simulator.tick(payload.get("seconds"))
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
            try:
                now = time.monotonic()
                elapsed = min(max(now - server._last_tick, 0.0), 10.0)
                server._last_tick = now
                if elapsed:
                    server.simulator.tick(elapsed)
                raw = [registers[10].value, registers[11].value]
                encoded_command = int(raw[0]).to_bytes(2, "big") + int(raw[1]).to_bytes(2, "big")
                command_value = struct.unpack(">f", encoded_command)[0]
                if command_value != server._last_command:
                    server._last_command = command_value
                    server.apply_power_setpoint(command_value)
            except (ConnectionError, ValueError, OverflowError, struct.error):
                server.simulator.set_fault("rejected")
            state = server.simulator.snapshot()
            for address, value in (
                (0, state.soc_kwh),
                (2, state.power_kw),
                (4, state.capacity_kwh),
            ):
                encoded = struct.pack(">f", float(value))
                registers[address].value = int.from_bytes(encoded[:2], "big")
                registers[address + 1].value = int.from_bytes(encoded[2:], "big")

        config = {
            "setup": {
                "di size": 0,
                "co size": 0,
                "ir size": 8,
                "hr size": 16,
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
            "write": [[10, 11]],
            "bits": [],
            "uint16": [],
            "uint32": [],
            "float32": [
                {"addr": [0, 1], "value": 5.0, "action": "battery_state"},
                {"addr": [2, 3], "value": 0.0, "action": "battery_state"},
                {"addr": [4, 5], "value": 10.0, "action": "battery_state"},
                {"addr": [10, 11], "value": 0.0},
            ],
            "string": [],
            "repeat": [],
        }
        return ModbusServerContext(
            ModbusSimulatorContext(config, {"battery_state": update_registers})
        )


async def main() -> None:
    service = BatteryLabServer(Path(os.getenv("BATTERY_PROFILE", "/app/profile.json")))
    app = web.Application()
    app.add_routes(
        [
            web.get("/health", service.health),
            web.get("/state", service.state),
            web.post("/command", service.command),
            web.post("/tick", service.tick_endpoint),
            web.post("/fault", service.fault),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("BATTERY_HTTP_PORT", "8090")))
    await site.start()
    await service.start_mqtt()
    modbus_task = asyncio.create_task(
        StartAsyncTcpServer(
            service.modbus_context(),
            address=("0.0.0.0", int(os.getenv("BATTERY_MODBUS_PORT", "1502"))),
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
