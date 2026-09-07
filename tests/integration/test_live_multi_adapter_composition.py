"""Opt-in end-to-end verification against the disposable lab stack."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import Command, CommandPostcondition, Plan

TARGETS = {
    "home_assistant": "switch.virtual_living_room_switch",
    "zigbee2mqtt": "garden/pump",
    "knx": "living_room.main_light",
    "modbus": "living_room.main_light",
}


@pytest.mark.asyncio
async def test_live_multi_adapter_command_round_trip(tmp_path: Path) -> None:
    if os.getenv("DOMOAI_LIVE_COMPOSITION_ENABLE") != "1":
        pytest.skip("set DOMOAI_LIVE_COMPOSITION_ENABLE=1 for the real lab composition")

    required = {
        "DOMOAI_HOME_ASSISTANT_URL": os.getenv("DOMOAI_HOME_ASSISTANT_URL"),
        "DOMOAI_HOME_ASSISTANT_TOKEN": os.getenv("DOMOAI_HOME_ASSISTANT_TOKEN"),
        "DOMOAI_ZIGBEE2MQTT_URL": os.getenv("DOMOAI_ZIGBEE2MQTT_URL"),
        "DOMOAI_MATTER_SERVER_URL": os.getenv("DOMOAI_MATTER_SERVER_URL"),
        "DOMOAI_KNX_GATEWAY_HOST": os.getenv("DOMOAI_KNX_GATEWAY_HOST"),
        "DOMOAI_KNX_CONFIG_PATH": os.getenv("DOMOAI_KNX_CONFIG_PATH"),
        "DOMOAI_MODBUS_HOST": os.getenv("DOMOAI_MODBUS_HOST"),
        "DOMOAI_MODBUS_CONFIG_PATH": os.getenv("DOMOAI_MODBUS_CONFIG_PATH"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        pytest.skip("live lab configuration is incomplete: " + ", ".join(missing))

    runtime = await build_runtime(
        Settings.from_environment().model_copy(update={"database_path": tmp_path / "live.sqlite3"})
    )
    event_task = asyncio.create_task(
        runtime.event_consumer.run(reconnect_delay=0.2, max_reconnect_delay=1.0)
    )
    turned_on: list[str] = []

    async def execute_power(device_id: str, desired: bool) -> None:
        suffix = "on" if desired else "off"
        plan_id = f"live-composition-{suffix}-{uuid.uuid4().hex}"
        command = Command(
            id=f"{plan_id}:command",
            device_id=device_id,
            command="turn_on" if desired else "turn_off",
            idempotency_key=f"{plan_id}:intent",
            postconditions=[
                CommandPostcondition(
                    capability="power",
                    expected=desired,
                    settle_timeout_seconds=10.0,
                    poll_interval_seconds=0.25,
                )
            ],
        )
        validated = runtime.facade.validate_plan(Plan(id=plan_id, commands=[command]))
        summary = await runtime.facade.execute_plan(validated)
        assert len(summary.outcomes) == 1
        outcome = summary.outcomes[0]
        assert outcome.status.value == "confirmed_success", outcome.error
        assert outcome.after_state is not None
        assert outcome.after_state.value is desired

    try:
        health = await runtime.adapter.health()
        assert health.components is not None
        assert {component.adapter_id for component in health.components} == {
            "home_assistant",
            "zigbee2mqtt",
            "matter",
            "knx",
            "modbus",
        }
        assert all(component.connected for component in health.components)

        targets: dict[str, str] = {}
        for adapter_id, external_id in TARGETS.items():
            device = next(
                (
                    item
                    for item in runtime.registry.devices
                    if any(
                        ref.adapter_id == adapter_id and ref.external_id == external_id
                        for ref in item.source_refs
                    )
                ),
                None,
            )
            assert device is not None, f"missing live target {adapter_id}:{external_id}"
            targets[adapter_id] = device.id

        matter_device = next(
            (
                item
                for item in runtime.registry.devices
                if item.availability == "available"
                if any(ref.adapter_id == "matter" for ref in item.source_refs)
                and any(
                    capability.name == "power"
                    and "turn_on" in capability.commands
                    and "turn_off" in capability.commands
                    for capability in item.capabilities
                )
            ),
            None,
        )
        assert matter_device is not None, "missing live Matter on/off target"
        targets["matter"] = matter_device.id

        for _adapter_id, device_id in targets.items():
            await execute_power(device_id, True)
            turned_on.append(device_id)
            await execute_power(device_id, False)
            turned_on.remove(device_id)

        assert runtime.event_consumer.alive is True
        assert runtime.event_consumer.events_applied > 0
        assert runtime.adapter.diagnostics == []
    finally:
        for device_id in turned_on:
            try:
                await execute_power(device_id, False)
            except Exception:
                pass
        event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)
        await runtime.close()
