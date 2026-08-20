"""Opt-in hardware-in-the-loop verification of real command execution against the lab."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import Command, ExecutionStatus, Plan


@pytest.mark.asyncio
async def test_home_assistant_provider_hil_command_round_trip(tmp_path: Path) -> None:
    base_url = os.getenv("DOMOAI_HOME_ASSISTANT_URL")
    token = os.getenv("DOMOAI_HOME_ASSISTANT_TOKEN")
    if not base_url or not token:
        pytest.skip(
            "Set DOMOAI_HOME_ASSISTANT_URL and DOMOAI_HOME_ASSISTANT_TOKEN "
            "for the Home Assistant Provider HIL command verification"
        )

    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "home-assistant-provider-hil.sqlite3",
            home_assistant_url=base_url,
            home_assistant_token=SecretStr(token),
        )
    )
    try:
        device = next(
            (
                device
                for device in runtime.registry.devices
                if any(
                    ref.external_id == "switch.virtual_living_room_switch"
                    for ref in device.source_refs
                )
            ),
            None,
        )
        assert device is not None, (
            "switch.virtual_living_room_switch must be discoverable on the live lab"
        )

        turn_on_plan = Plan(
            id="hil-turn-on-1",
            commands=[
                Command(
                    id="hil-turn-on-1:command",
                    device_id=device.id,
                    command="turn_on",
                    idempotency_key="hil-turn-on-1:intent",
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
                id="hil-turn-off-1",
                commands=[
                    Command(
                        id="hil-turn-off-1:command",
                        device_id=device.id,
                        command="turn_off",
                        idempotency_key="hil-turn-off-1:intent",
                    )
                ],
            )
            restored = runtime.facade.validate_plan(turn_off_plan)
            await runtime.facade.execute_plan(restored)
    finally:
        await runtime.close()
