"""Opt-in hardware-in-the-loop verification of real command execution against KNX."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import Command, ExecutionStatus, Plan


@pytest.mark.asyncio
async def test_knx_hil_command_round_trip(tmp_path: Path) -> None:
    host = os.getenv("DOMOAI_KNX_GATEWAY_HOST")
    mapping_path = os.getenv("DOMOAI_KNX_CONFIG_PATH")
    if not host or not mapping_path or not Path(mapping_path).exists():
        pytest.skip("KNX HIL verification requires an explicit gateway and mapping file")

    timeout = float(os.getenv("DOMOAI_KNX_TIMEOUT_SECONDS", "5"))
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "knx-hil.sqlite3",
            knx_gateway_host=host,
            knx_config_path=Path(mapping_path),
            knx_timeout_seconds=timeout,
        )
    )
    event_task = asyncio.create_task(
        runtime.event_consumer.run(reconnect_delay=0.2, max_reconnect_delay=1.0)
    )
    try:
        device = next(
            (
                device
                for device in runtime.registry.devices
                if any(ref.external_id == "living_room.main_light" for ref in device.source_refs)
            ),
            None,
        )
        assert device is not None, "living_room.main_light must be discoverable on the live KNX bus"

        turn_on_plan = Plan(
            id="knx-hil-turn-on-1",
            commands=[
                Command(
                    id="knx-hil-turn-on-1:command",
                    device_id=device.id,
                    command="turn_on",
                    idempotency_key="knx-hil-turn-on-1:intent",
                )
            ],
        )

        try:
            validated = runtime.facade.validate_plan(turn_on_plan)
            summary = await runtime.facade.execute_plan(validated)

            assert len(summary.outcomes) == 1, "build stage failure: no outcome produced"
            outcome = summary.outcomes[0]
            assert outcome.status is ExecutionStatus.CONFIRMED_SUCCESS, (
                f"execute stage failure: {outcome.status}, error={outcome.error}"
            )
            assert outcome.after_state is not None and outcome.after_state.value is True, (
                "postcondition stage failure: after_state did not confirm power=on"
            )

            readback = await runtime.adapter.read_state(device.source_refs)
            power_readback = next(
                (state for state in readback if state.capability == "power"), None
            )
            assert power_readback is not None and power_readback.value is True, (
                "readback stage failure: independent re-read did not confirm power=on"
            )
        finally:
            turn_off_plan = Plan(
                id="knx-hil-turn-off-1",
                commands=[
                    Command(
                        id="knx-hil-turn-off-1:command",
                        device_id=device.id,
                        command="turn_off",
                        idempotency_key="knx-hil-turn-off-1:intent",
                    )
                ],
            )
            restored = runtime.facade.validate_plan(turn_off_plan)
            await runtime.facade.execute_plan(restored)
    finally:
        event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)
        await runtime.close()
