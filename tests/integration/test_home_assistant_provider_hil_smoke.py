"""Opt-in hardware-in-the-loop verification of real command execution against the lab."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.application.battery_composition import (
    compose_home_assistant_dispatchable_battery_binding,
)
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import (
    Command,
    CommandPostcondition,
    ExecutionStatus,
    Plan,
    RiskClass,
    StateStatus,
)
from domoai.optimizer.energy import (
    BatteryCapacityEvidence,
    BatteryProfile,
    NominalCapacityTrustPolicy,
)


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


def _battery_hil_inputs() -> dict[str, Any]:
    required = {
        "base_url": os.getenv("DOMOAI_HOME_ASSISTANT_URL"),
        "token": os.getenv("DOMOAI_HOME_ASSISTANT_TOKEN"),
        "mapping_path": os.getenv("DOMOAI_HOME_ASSISTANT_MAPPING_PATH"),
        "profile_path": os.getenv("DOMOAI_BATTERY_HIL_PROFILE_PATH"),
        "canonical_device_id": os.getenv("DOMOAI_BATTERY_HIL_CANONICAL_DEVICE_ID"),
        "binding_id": os.getenv("DOMOAI_BATTERY_HIL_BINDING_ID"),
        "direction": os.getenv("DOMOAI_BATTERY_HIL_DIRECTION"),
        "operator_token": os.getenv("DOMOAI_BATTERY_HIL_OPERATOR_TOKEN"),
    }
    if (
        os.getenv("DOMOAI_BATTERY_HIL_ENABLE") != "1"
        or os.getenv("DOMOAI_BATTERY_HIL_CONFIRM") != "I_UNDERSTAND_REAL_BATTERY_HIL"
        or any(not isinstance(value, str) or not value for value in required.values())
    ):
        pytest.skip(
            "Real battery HIL requires explicit enable, confirmation, HA credentials, "
            "mapping/profile paths, canonical device, binding, direction and operator token"
        )
    mapping_path = Path(cast(str, required["mapping_path"]))
    profile_path = Path(cast(str, required["profile_path"]))
    if not mapping_path.is_file() or not profile_path.is_file():
        pytest.skip("Real battery HIL requires existing mapping and profile files")
    direction = required["direction"]
    if direction not in {"charge", "discharge"}:
        pytest.skip("DOMOAI_BATTERY_HIL_DIRECTION must be charge or discharge")
    try:
        probe_kw = float(os.environ["DOMOAI_BATTERY_HIL_PROBE_KW"])
    except (KeyError, ValueError):
        pytest.skip("DOMOAI_BATTERY_HIL_PROBE_KW must be a positive number")
    if not math.isfinite(probe_kw) or probe_kw <= 0:
        pytest.skip("DOMOAI_BATTERY_HIL_PROBE_KW must be a positive finite number")
    return {
        **required,
        "mapping_path": mapping_path,
        "profile_path": profile_path,
        "probe_kw": probe_kw,
    }


def _load_hil_binding(
    profile_path: Path, *, canonical_device_id: str
) -> tuple[BatteryProfile, BatteryCapacityEvidence, NominalCapacityTrustPolicy | None]:
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("battery HIL profile must be a JSON object")
    profile = BatteryProfile.model_validate(payload["profile"])
    evidence = BatteryCapacityEvidence.model_validate(payload["capacity_evidence"])
    policy_payload = payload.get("capacity_trust_policy")
    policy = (
        NominalCapacityTrustPolicy.model_validate(policy_payload)
        if policy_payload is not None
        else None
    )
    if profile.actuator is None or profile.actuator.device_id != canonical_device_id:
        raise ValueError("battery HIL profile actuator must use the canonical device ID")
    if evidence.device_id != canonical_device_id:
        raise ValueError("battery HIL capacity evidence must use the canonical device ID")
    return profile, evidence, policy


def _route_or_fail(runtime: Any, device_id: str, capability: str) -> Any:
    routes = [
        route
        for route in runtime.registry.routes_for(device_id, capability)
        if route.available
    ]
    assert len(routes) == 1, f"expected one available {capability} route"
    return routes[0]


async def _fresh_power_and_soc(
    runtime: Any, device_id: str, power_capability: str
) -> tuple[float, float]:
    power_route = _route_or_fail(runtime, device_id, power_capability)
    soc_route = _route_or_fail(runtime, device_id, "battery.soc")
    states = await runtime.adapter.read_state([power_route.source_ref, soc_route.source_ref])
    power = next(state for state in states if state.capability == power_capability)
    soc = next(state for state in states if state.capability == "battery.soc")
    assert power.status is StateStatus.CURRENT
    assert soc.status is StateStatus.CURRENT
    assert isinstance(power.value, (int, float)) and not isinstance(power.value, bool)
    assert isinstance(soc.value, (int, float)) and not isinstance(soc.value, bool)
    assert math.isfinite(float(power.value))
    assert 0 <= float(soc.value) <= 100
    return float(power.value), float(soc.value)


async def _wait_for_convergent_readback(
    runtime: Any,
    device_id: str,
    power_capability: str,
    expected_power: float,
    tolerance: float,
    timeout: float,
    poll_interval: float,
) -> tuple[float, float]:
    deadline = asyncio.get_running_loop().time() + timeout
    previous: tuple[float, float] | None = None
    latest: tuple[float, float] | None = None
    while True:
        latest = await _fresh_power_and_soc(runtime, device_id, power_capability)
        if (
            abs(latest[0] - expected_power) <= tolerance
            and previous is not None
            and abs(latest[0] - previous[0]) <= tolerance
            and abs(latest[1] - previous[1]) <= max(0.5, tolerance)
        ):
            return latest
        previous = latest
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "battery readback did not converge: "
                f"expected power {expected_power}, latest {latest}"
            )
        await asyncio.sleep(poll_interval)


@pytest.mark.asyncio
async def test_home_assistant_inverter_hil_power_and_soc_converge(tmp_path: Path) -> None:
    """Opt-in physical smoke; default CI must skip before runtime/service I/O."""

    values = _battery_hil_inputs()
    profile, capacity_evidence, trust_policy = _load_hil_binding(
        values["profile_path"], canonical_device_id=values["canonical_device_id"]
    )
    assert profile.actuator is not None
    actuator = profile.actuator
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "home-assistant-inverter-hil.sqlite3",
            home_assistant_url=values["base_url"],
            home_assistant_token=SecretStr(values["token"]),
            home_assistant_mapping_path=values["mapping_path"],
            operator_approval_token=SecretStr(values["operator_token"]),
        )
    )
    probe_started = False
    try:
        provider = runtime.provider_registry.get("home_assistant")
        assert isinstance(provider, HomeAssistantProvider)
        source_snapshot = await provider.snapshot()
        binding = compose_home_assistant_dispatchable_battery_binding(
            provider,
            source_snapshot,
            binding_id=values["binding_id"],
            canonical_device_id=values["canonical_device_id"],
            profile=profile,
            capacity_evidence=capacity_evidence,
            capacity_trust_policy=trust_policy,
        )
        assert binding.device_id == values["canonical_device_id"]
        assert runtime.registry.get(values["canonical_device_id"]) is not None

        direction = values["direction"]
        probe_kw = values["probe_kw"]
        maximum = profile.max_charge_kw if direction == "charge" else profile.max_discharge_kw
        assert probe_kw <= maximum, "HIL probe exceeds the server-owned battery profile limit"
        command_name = (
            actuator.charge_command if direction == "charge" else actuator.discharge_command
        )
        expected_power = probe_kw if direction == "charge" else -probe_kw
        if actuator.power_feedback_convention == "discharge_positive":
            expected_power = -expected_power
        before_power, before_soc = await _fresh_power_and_soc(
            runtime, binding.device_id, actuator.power_feedback_capability
        )
        assert math.isfinite(before_power) and math.isfinite(before_soc)
        plan = Plan(
            id=f"battery-hil-{direction}",
            commands=[
                Command(
                    id=f"battery-hil-{direction}:command",
                    device_id=binding.device_id,
                    command=command_name,
                    value=probe_kw,
                    unit=actuator.power_unit,
                    risk_class=RiskClass.CONFIRM,
                    idempotency_key=f"battery-hil-{direction}:intent",
                    postconditions=[
                        CommandPostcondition(
                            capability=actuator.power_feedback_capability,
                            expected=expected_power,
                            tolerance=actuator.power_feedback_tolerance_kw,
                            settle_timeout_seconds=actuator.power_feedback_settle_timeout_seconds,
                            poll_interval_seconds=actuator.power_feedback_poll_interval_seconds,
                            reconcile_capabilities=["battery.soc"],
                        )
                    ],
                )
            ],
        )
        validated = runtime.facade.validate_plan(plan)
        assert validated.status.value == "requires_confirmation"
        grant = runtime.approval_store.issue(
            validated,
            approved_by="real_battery_hil_operator",
            operator_token=values["operator_token"],
        )
        approved = runtime.facade.plan_service.approve(validated, grant=grant)
        probe_started = True
        summary = await runtime.facade.execute_plan(approved)
        assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
        power_after, soc_after = await _wait_for_convergent_readback(
            runtime,
            binding.device_id,
            actuator.power_feedback_capability,
            expected_power,
            actuator.power_feedback_tolerance_kw,
            max(5.0, actuator.power_feedback_settle_timeout_seconds),
            actuator.power_feedback_poll_interval_seconds,
        )
        assert abs(power_after - expected_power) <= actuator.power_feedback_tolerance_kw
        assert 0 <= soc_after <= 100
    finally:
        if probe_started:
            stop_plan = Plan(
                id="battery-hil-stop",
                commands=[
                    Command(
                        id="battery-hil-stop:command",
                        device_id=values["canonical_device_id"],
                        command=actuator.stop_command,
                        risk_class=RiskClass.CONFIRM,
                        idempotency_key="battery-hil-stop:intent",
                        postconditions=[
                            CommandPostcondition(
                                capability=actuator.power_feedback_capability,
                                expected=0,
                                tolerance=actuator.power_feedback_tolerance_kw,
                                settle_timeout_seconds=max(
                                    5.0, actuator.power_feedback_settle_timeout_seconds
                                ),
                                poll_interval_seconds=actuator.power_feedback_poll_interval_seconds,
                            )
                        ],
                    )
                ],
            )
            validated_stop = runtime.facade.validate_plan(stop_plan)
            grant_stop = runtime.approval_store.issue(
                validated_stop,
                approved_by="real_battery_hil_operator",
                operator_token=values["operator_token"],
            )
            approved_stop = runtime.facade.plan_service.approve(validated_stop, grant=grant_stop)
            await runtime.facade.execute_plan(approved_stop)
        await runtime.close()
