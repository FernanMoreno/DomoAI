from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from domoai.adapters.knx.config import load_mapping
from domoai.lab.bridge_supervisor import BridgeState, BridgeStatusStore
from domoai.lab.knx_bridge import knx_command_to_mqtt, state_to_knx_writes

ROOT = Path(__file__).parents[3]
BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "domoai_lab_battery_knx_bridge", ROOT / "dev/lab/battery/knx_bridge.py"
)
assert BRIDGE_SPEC is not None and BRIDGE_SPEC.loader is not None
knx_bridge = importlib.util.module_from_spec(BRIDGE_SPEC)
sys.modules[BRIDGE_SPEC.name] = knx_bridge
BRIDGE_SPEC.loader.exec_module(knx_bridge)

MAPPING = load_mapping(Path("dev/lab/configs/knx-battery-virtual.json"))


def test_state_projects_to_configured_knx_groups() -> None:
    writes = state_to_knx_writes({"soc_kwh": 5.0, "power_kw": -1.5, "capacity_kwh": 10.0}, MAPPING)

    assert [(item.group_address, item.dpt, item.value) for item in writes] == [
        ("4/0/2", "9.024", -1.5),
        ("4/0/1", "13.013", 5.0),
        ("4/0/3", "13.013", 10.0),
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2, b"2"), (-1.5, b"-1.5"), (0.0, b"0")],
)
def test_knx_command_is_a_signed_mqtt_setpoint(value: float, expected: bytes) -> None:
    assert knx_command_to_mqtt(value) == expected


def test_bridge_rejects_incomplete_state() -> None:
    with pytest.raises(ValueError, match="soc_kwh"):
        state_to_knx_writes({"soc_kwh": None}, MAPPING)


def test_bridge_rejects_boolean_command() -> None:
    with pytest.raises(ValueError, match="numeric"):
        knx_command_to_mqtt(True)


@dataclass
class Message:
    topic: str
    payload: bytes


class FailingMqtt:
    def __init__(self, *_args, **_kwargs) -> None:
        self.disconnect_called = False
        self.received = False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_called = True

    async def subscribe(self, _topic: str) -> None:
        return None

    async def publish(self, _topic: str, _payload: bytes) -> None:
        return None

    async def receive(self, timeout: float | None = None) -> Message:
        await asyncio.sleep(0)
        if not self.received:
            self.received = True
            return Message(
                "domoai/battery/state",
                b'{"soc_kwh": 5, "power_kw": 0, "capacity_kwh": 10}',
            )
        raise RuntimeError("synthetic mqtt failure")


class ReadyMqtt:
    def __init__(self, *_args, **_kwargs) -> None:
        self.disconnect_called = False
        self.received = False
        self._block = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_called = True

    async def subscribe(self, _topic: str) -> None:
        return None

    async def publish(self, _topic: str, _payload: bytes) -> None:
        return None

    async def receive(self, *, timeout: float | None = None) -> Message:
        await asyncio.sleep(0)
        if not self.received:
            self.received = True
            return Message(
                "domoai/battery/state",
                b'{"soc_kwh": 5, "power_kw": 0, "capacity_kwh": 10}',
            )
        await self._block.wait()
        raise AssertionError("unreachable")


class BlockingKnx:
    def __init__(self, *_args, **_kwargs) -> None:
        self.disconnect_called = False
        self.cancelled = False
        self._block = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_called = True

    def set_group_read_response(self, _address: str, _dpt: str, _value: object) -> None:
        return None

    async def write_group(self, _address: str, _dpt: str, _value: object) -> None:
        return None

    async def receive(self, *, timeout: float | None = None) -> None:
        try:
            await self._block.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class ReconnectingMqtt:
    attempts = 0

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).attempts += 1
        self.attempt = type(self).attempts
        self.disconnect_called = False
        self.received = False
        self._block = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_called = True

    async def subscribe(self, _topic: str) -> None:
        return None

    async def publish(self, _topic: str, _payload: bytes) -> None:
        return None

    async def receive(self, timeout: float | None = None) -> Message:
        await asyncio.sleep(0)
        if not self.received:
            self.received = True
            return Message(
                "domoai/battery/state",
                b'{"soc_kwh": 5, "power_kw": 0, "capacity_kwh": 10}',
            )
        if self.attempt == 1:
            raise ConnectionError("synthetic broker disconnect")
        await self._block.wait()
        raise AssertionError("unreachable")


class ReconnectingKnx(BlockingKnx):
    attempts = 0

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__(*_args, **_kwargs)
        type(self).attempts += 1
        self.attempt = type(self).attempts

    async def receive(self, *, timeout: float | None = None) -> None:
        if self.attempt == 1:
            await asyncio.sleep(0)
            raise ConnectionError("synthetic KNX gateway disconnect")
        await super().receive(timeout=timeout)


@pytest.mark.asyncio
async def test_bridge_cancels_the_other_loop_when_one_transport_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mqtt = FailingMqtt()
    knx = BlockingKnx()
    monkeypatch.setattr(knx_bridge, "AiomqttTransport", lambda *args, **kwargs: mqtt)
    monkeypatch.setattr(knx_bridge, "XknxTransport", lambda *args, **kwargs: knx)
    status_path = tmp_path / "bridge-status.json"

    with pytest.raises(RuntimeError, match="synthetic mqtt failure"):
        await knx_bridge.run_bridge(
            knx_bridge.BatteryBridgeConfig(
                mapping_path=Path("dev/lab/configs/knx-battery-virtual.json"),
                knx_host="172.26.80.1",
                timeout_seconds=0.05,
                status_path=status_path,
            )
        )

    assert knx.cancelled is True
    assert mqtt.disconnect_called is True
    assert knx.disconnect_called is True
    status = BridgeStatusStore(status_path).read()
    assert status is not None
    assert status.state is BridgeState.FAILED


@pytest.mark.asyncio
async def test_bridge_reconnects_after_transient_transport_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ReconnectingMqtt.attempts = 0
    mqtt_instances: list[ReconnectingMqtt] = []
    knx_instances: list[BlockingKnx] = []

    def mqtt_factory(*args, **kwargs) -> ReconnectingMqtt:
        instance = ReconnectingMqtt(*args, **kwargs)
        mqtt_instances.append(instance)
        return instance

    def knx_factory(*args, **kwargs) -> BlockingKnx:
        instance = BlockingKnx(*args, **kwargs)
        knx_instances.append(instance)
        return instance

    monkeypatch.setattr(knx_bridge, "AiomqttTransport", mqtt_factory)
    monkeypatch.setattr(knx_bridge, "XknxTransport", knx_factory)
    status_path = tmp_path / "bridge-status.json"
    task = asyncio.create_task(
        knx_bridge.run_bridge(
            knx_bridge.BatteryBridgeConfig(
                mapping_path=Path("dev/lab/configs/knx-battery-virtual.json"),
                knx_host="172.26.80.1",
                timeout_seconds=0.05,
                status_path=status_path,
                reconnect_initial_delay_seconds=0.001,
                reconnect_max_delay_seconds=0.001,
            )
        )
    )

    for _ in range(200):
        if len(mqtt_instances) >= 2:
            break
        await asyncio.sleep(0.001)
    else:
        pytest.fail("bridge did not create a replacement transport")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status = BridgeStatusStore(status_path).read()
    assert status is not None
    assert status.state is BridgeState.STOPPED
    assert mqtt_instances[0].disconnect_called is True
    assert knx_instances[0].disconnect_called is True
    assert len(knx_instances) >= 2


@pytest.mark.asyncio
async def test_bridge_reconnects_after_transient_knx_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ReconnectingKnx.attempts = 0
    mqtt_instances: list[ReadyMqtt] = []
    knx_instances: list[ReconnectingKnx] = []

    def mqtt_factory(*args, **kwargs) -> ReadyMqtt:
        instance = ReadyMqtt(*args, **kwargs)
        mqtt_instances.append(instance)
        return instance

    def knx_factory(*args, **kwargs) -> ReconnectingKnx:
        instance = ReconnectingKnx(*args, **kwargs)
        knx_instances.append(instance)
        return instance

    monkeypatch.setattr(knx_bridge, "AiomqttTransport", mqtt_factory)
    monkeypatch.setattr(knx_bridge, "XknxTransport", knx_factory)
    status_path = tmp_path / "bridge-status.json"
    task = asyncio.create_task(
        knx_bridge.run_bridge(
            knx_bridge.BatteryBridgeConfig(
                mapping_path=Path("dev/lab/configs/knx-battery-virtual.json"),
                knx_host="172.26.80.1",
                timeout_seconds=0.05,
                status_path=status_path,
                reconnect_initial_delay_seconds=0.001,
                reconnect_max_delay_seconds=0.001,
            )
        )
    )

    for _ in range(200):
        if len(knx_instances) >= 2:
            break
        await asyncio.sleep(0.001)
    else:
        pytest.fail("bridge did not create a replacement KNX transport")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status = BridgeStatusStore(status_path).read()
    assert status is not None
    assert status.state is BridgeState.STOPPED
    assert len(mqtt_instances) >= 2
    assert knx_instances[0].disconnect_called is True


@pytest.mark.asyncio
async def test_bridge_reports_projected_state_only_after_complete_retained_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mqtt = ReadyMqtt()
    knx = BlockingKnx()
    monkeypatch.setattr(knx_bridge, "AiomqttTransport", lambda *args, **kwargs: mqtt)
    monkeypatch.setattr(knx_bridge, "XknxTransport", lambda *args, **kwargs: knx)
    status_path = tmp_path / "bridge-status.json"
    task = asyncio.create_task(
        knx_bridge.run_bridge(
            knx_bridge.BatteryBridgeConfig(
                mapping_path=Path("dev/lab/configs/knx-battery-virtual.json"),
                knx_host="172.26.80.1",
                timeout_seconds=0.05,
                status_path=status_path,
            )
        )
    )

    for _ in range(100):
        status = BridgeStatusStore(status_path).read()
        if status is not None and status.last_state_at is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("bridge did not report projected state after receiving retained state")

    status = BridgeStatusStore(status_path).read()
    assert status is not None
    assert status.state is BridgeState.DEGRADED
    assert status.knx_readback_ok is None
    assert status.message == "battery state projected; awaiting supervisor readback"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stopped = BridgeStatusStore(status_path).read()
    assert stopped is not None
    assert stopped.state is BridgeState.STOPPED
