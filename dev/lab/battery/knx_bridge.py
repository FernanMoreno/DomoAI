"""Bridge the deterministic MQTT battery facade to KNX Virtual.

KNX Virtual runs on Windows, outside the Docker Compose network.  This small
host-side process deliberately keeps the boundary explicit:

* MQTT state -> KNX group-value writes;
* KNX battery power command -> MQTT power setpoint.

It is a laboratory bridge, not an actuator-authority grant.  Production
execution must still pass through DomoAI's admission, safety and lease gates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from domoai.adapters.knx.config import KnxMappingDocument, load_mapping
from domoai.adapters.knx.transport import KnxTransport, XknxTransport
from domoai.adapters.zigbee2mqtt.transport import AiomqttTransport, MqttTransport
from domoai.lab.bridge_supervisor import (
    BridgeState,
    BridgeStatus,
    BridgeStatusStore,
    mapping_digest,
)
from domoai.lab.knx_bridge import knx_command_to_mqtt, state_to_knx_writes


@dataclass(frozen=True)
class BatteryBridgeConfig:
    mapping_path: Path
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topic: str = "domoai/battery"
    knx_host: str = "172.26.93.253"
    knx_port: int = 3672
    knx_route_back: bool = False
    timeout_seconds: float = 5.0
    status_path: Path | None = None
    reconnect_initial_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("bridge timeout must be positive")
        if self.reconnect_initial_delay_seconds <= 0:
            raise ValueError("bridge reconnect initial delay must be positive")
        if self.reconnect_max_delay_seconds < self.reconnect_initial_delay_seconds:
            raise ValueError("bridge reconnect max delay must not be below initial delay")


def _group_dpts(mapping: KnxMappingDocument) -> dict[str, str]:
    return {
        address: binding.dpt
        for entity in mapping.entities
        for binding in entity.capabilities
        for address in (binding.state_group_address, binding.command_group_address)
        if address is not None
    }


def _command_group(mapping: KnxMappingDocument) -> str:
    for entity in mapping.entities:
        for binding in entity.capabilities:
            if binding.name == "battery.power" and binding.command_group_address is not None:
                return binding.command_group_address
    raise ValueError("KNX mapping has no battery.power command group")


async def _wait_for_bridge_pumps(tasks: list[asyncio.Task[None]]) -> None:
    """Wait for either pump to stop and preserve its failure for supervision."""

    done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finished = next(iter(done))
    if finished.cancelled():
        raise asyncio.CancelledError
    error = finished.exception()
    if error is not None:
        raise error
    raise ConnectionError("bridge transport loop ended unexpectedly")


async def _run_bridge_session(
    config: BatteryBridgeConfig,
    mapping: KnxMappingDocument,
    publish_status: Callable[..., None],
) -> None:
    """Run one connected session; transport errors are handled by the caller."""

    mqtt = AiomqttTransport(
        config.mqtt_host,
        port=config.mqtt_port,
        timeout=config.timeout_seconds,
    )
    knx = XknxTransport(
        config.knx_host,
        gateway_port=config.knx_port,
        route_back=config.knx_route_back,
        timeout=config.timeout_seconds,
        group_dpts=_group_dpts(mapping),
    )
    tasks: list[asyncio.Task[None]] = []

    async def disconnect(transport: MqttTransport | KnxTransport) -> None:
        try:
            await transport.disconnect()
        except Exception:
            # A broken transport is already being replaced.  Cleanup must not
            # hide the original disconnect/reconnect cause.
            return

    try:
        await mqtt.connect()
        await knx.connect()
        await mqtt.subscribe(f"{config.mqtt_topic}/state")
        command_group = _command_group(mapping)
        state_ready = asyncio.Event()
        publish_status(BridgeState.DEGRADED, message="waiting for retained battery state")

        async def mqtt_to_knx() -> None:
            while True:
                message = await mqtt.receive(timeout=1.0)
                if message is None or message.topic != f"{config.mqtt_topic}/state":
                    continue
                payload = json.loads(message.payload.decode("utf-8"))
                for write in state_to_knx_writes(payload, mapping):
                    knx.set_group_read_response(write.group_address, write.dpt, write.value)
                    await knx.write_group(write.group_address, write.dpt, write.value)
                observed_at = _utc_now()
                publish_status(
                    BridgeState.DEGRADED,
                    message="battery state projected; awaiting supervisor readback",
                    last_state_at=observed_at,
                )
                state_ready.set()

        async def knx_to_mqtt() -> None:
            while True:
                value = await knx.receive(timeout=1.0)
                if value is None or value.group_address != command_group:
                    continue
                await mqtt.publish(
                    f"{config.mqtt_topic}/power/set", knx_command_to_mqtt(value.value)
                )

        tasks = [asyncio.create_task(mqtt_to_knx()), asyncio.create_task(knx_to_mqtt())]
        try:
            await asyncio.wait_for(state_ready.wait(), config.timeout_seconds)
        except TimeoutError:
            publish_status(
                BridgeState.DEGRADED,
                error_code="retained_state_unavailable",
                message="no complete retained battery state",
            )
        print(
            f"battery KNX bridge connected mqtt={config.mqtt_host}:{config.mqtt_port} "
            f"knx={config.knx_host}:{config.knx_port} command_group={command_group}",
            flush=True,
        )
        await _wait_for_bridge_pumps(tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await disconnect(knx)
        await disconnect(mqtt)


async def run_bridge(config: BatteryBridgeConfig) -> None:
    store = BridgeStatusStore(config.status_path) if config.status_path is not None else None
    started_at = _utc_now()
    mapping: KnxMappingDocument | None = None

    def publish_status(
        state: BridgeState,
        *,
        message: str | None = None,
        error_code: str | None = None,
        last_state_at: str | None = None,
    ) -> None:
        if store is None:
            return
        current = store.read()
        status = BridgeStatus(
            state=state,
            pid=os.getpid(),
            started_at=started_at,
            updated_at=_utc_now(),
            last_state_at=last_state_at
            if last_state_at is not None
            else current.last_state_at
            if current is not None
            else None,
            knx_readback_at=current.knx_readback_at if current is not None else None,
            knx_readback_ok=current.knx_readback_ok if current is not None else None,
            mapping_path=_mapping_display_path(config.mapping_path),
            mapping_digest=_mapping_digest(config.mapping_path),
            knx_host=config.knx_host,
            knx_port=config.knx_port,
            error_code=error_code,
            message=message,
        )
        store.write(status)

    publish_status(BridgeState.STARTING, message="bridge process starting")
    try:
        mapping = load_mapping(config.mapping_path)
    except asyncio.CancelledError:
        publish_status(BridgeState.STOPPED, message="bridge stopped")
        raise
    except Exception:
        publish_status(
            BridgeState.FAILED,
            error_code="bridge_runtime_error",
            message="bridge failed; inspect the local bridge log",
        )
        raise

    reconnect_delay = config.reconnect_initial_delay_seconds
    while True:
        try:
            await _run_bridge_session(config, mapping, publish_status)
        except asyncio.CancelledError:
            publish_status(BridgeState.STOPPED, message="bridge stopped")
            raise
        except (ConnectionError, OSError, TimeoutError) as error:
            next_delay = min(reconnect_delay, config.reconnect_max_delay_seconds)
            publish_status(
                BridgeState.DEGRADED,
                error_code="bridge_reconnecting",
                message=f"transport unavailable; retrying in {next_delay:g}s",
            )
            print(
                f"battery KNX bridge reconnecting after transport failure: {error}",
                flush=True,
            )
            await asyncio.sleep(next_delay)
            reconnect_delay = min(
                max(reconnect_delay * 2, config.reconnect_initial_delay_seconds),
                config.reconnect_max_delay_seconds,
            )
        except Exception:
            publish_status(
                BridgeState.FAILED,
                error_code="bridge_runtime_error",
                message="bridge failed; inspect the local bridge log",
            )
            raise


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mapping_display_path(path: Path) -> str:
    return path.as_posix()


def _mapping_digest(path: Path) -> str | None:
    try:
        return mapping_digest(path)
    except OSError:
        return None


def _parse_args() -> BatteryBridgeConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path("dev/lab/configs/knx-battery-virtual.json"),
    )
    parser.add_argument("--mqtt-host", default=os.getenv("DOMOAI_BATTERY_MQTT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=int(os.getenv("DOMOAI_BATTERY_MQTT_PORT", "1883")),
    )
    parser.add_argument(
        "--mqtt-topic", default=os.getenv("DOMOAI_BATTERY_MQTT_TOPIC", "domoai/battery")
    )
    parser.add_argument("--knx-host", default=os.getenv("DOMOAI_KNX_GATEWAY_HOST", "172.26.93.253"))
    parser.add_argument(
        "--knx-port",
        type=int,
        default=int(os.getenv("DOMOAI_KNX_GATEWAY_PORT", "3672")),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("DOMOAI_KNX_TIMEOUT_SECONDS", "5")),
    )
    parser.add_argument(
        "--knx-route-back",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("DOMOAI_KNX_ROUTE_BACK", "0").strip().lower()
        in {"1", "true", "yes", "on"},
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=(
            Path(os.environ["DOMOAI_KNX_BRIDGE_STATUS_PATH"])
            if os.getenv("DOMOAI_KNX_BRIDGE_STATUS_PATH")
            else None
        ),
    )
    parser.add_argument(
        "--reconnect-initial-delay",
        type=float,
        default=float(os.getenv("DOMOAI_KNX_BRIDGE_RECONNECT_INITIAL_DELAY_SECONDS", "1")),
    )
    parser.add_argument(
        "--reconnect-max-delay",
        type=float,
        default=float(os.getenv("DOMOAI_KNX_BRIDGE_RECONNECT_MAX_DELAY_SECONDS", "30")),
    )
    args = parser.parse_args()
    return BatteryBridgeConfig(
        mapping_path=args.mapping,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        mqtt_topic=args.mqtt_topic,
        knx_host=args.knx_host,
        knx_port=args.knx_port,
        knx_route_back=args.knx_route_back,
        timeout_seconds=args.timeout,
        status_path=args.status_file,
        reconnect_initial_delay_seconds=args.reconnect_initial_delay,
        reconnect_max_delay_seconds=args.reconnect_max_delay,
    )


if __name__ == "__main__":
    asyncio.run(run_bridge(_parse_args()))
