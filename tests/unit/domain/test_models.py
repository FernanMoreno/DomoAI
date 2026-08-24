from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domoai.domain.models import (
    AvailabilityStatus,
    Capability,
    CapabilityKind,
    Command,
    Device,
    DeviceType,
    Plan,
    Precondition,
    RiskClass,
    SourceRef,
    StateSnapshot,
    StateStatus,
)


def test_device_keeps_canonical_identity_and_source_reference() -> None:
    source = SourceRef(adapter_id="home_assistant", external_id="light.living_room")
    capability = Capability(
        name="brightness",
        kind=CapabilityKind.INTEGER,
        unit="%",
        readable=True,
        writable=True,
        minimum=0,
        maximum=100,
        commands=["set_brightness"],
    )

    device = Device(
        id="living_room.main_light",
        type=DeviceType.LIGHT,
        name="Main light",
        protocol="home_assistant",
        capabilities=[capability],
        source_refs=[source],
    )

    assert device.id == "living_room.main_light"
    assert device.source_refs[0].external_id == "light.living_room"
    assert device.availability is AvailabilityStatus.UNKNOWN


def test_capability_rejects_inverted_numeric_range() -> None:
    with pytest.raises(ValidationError, match="minimum"):
        Capability(
            name="brightness",
            kind=CapabilityKind.INTEGER,
            unit="%",
            readable=True,
            writable=True,
            minimum=100,
            maximum=0,
            commands=["set_brightness"],
        )


def test_capability_accepts_one_sided_numeric_bounds() -> None:
    capability = Capability(
        name="temperature",
        kind=CapabilityKind.NUMBER,
        readable=True,
        writable=True,
        maximum=32,
        commands=["set_temperature"],
    )

    assert capability.minimum is None
    assert capability.maximum == 32


def test_state_snapshot_requires_matching_observation_order() -> None:
    with pytest.raises(ValidationError, match="observed_at"):
        StateSnapshot(
            device_id="living_room.main_light",
            capability="brightness",
            value=50,
            observed_at=datetime(2026, 8, 15, 10, 1, tzinfo=UTC),
            received_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id="light.living_room"),
        )


def test_plan_rejects_more_than_fifty_commands() -> None:
    commands = [
        Command(
            id=f"command-{index}",
            device_id="living_room.main_light",
            command="turn_on",
            risk_class=RiskClass.SAFE,
            idempotency_key=f"intent-{index}",
        )
        for index in range(51)
    ]

    with pytest.raises(ValidationError, match="50"):
        Plan(id="plan-1", commands=commands)


def _single_command() -> list[Command]:
    return [
        Command(
            id="command-1",
            device_id="living_room.main_light",
            command="turn_on",
            idempotency_key="intent-1",
        )
    ]


def test_plan_rejects_naive_execute_at() -> None:
    with pytest.raises(ValidationError, match="execute_at"):
        Plan(
            id="plan-naive-execute-at",
            execute_at=datetime(2026, 8, 15, 10, 0, 0),
            commands=_single_command(),
        )


def test_plan_rejects_naive_expires_at() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        Plan(
            id="plan-naive-expires-at",
            expires_at=datetime(2026, 8, 15, 10, 0, 0),
            commands=_single_command(),
        )


def test_plan_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="created_at"):
        Plan(
            id="plan-naive-created-at",
            created_at=datetime(2026, 8, 15, 10, 0, 0),
            commands=_single_command(),
        )


def test_plan_accepts_aware_timestamps() -> None:
    plan = Plan(
        id="plan-aware-timestamps",
        created_at=datetime(2026, 8, 15, 9, 0, 0, tzinfo=UTC),
        expires_at=datetime(2026, 8, 15, 11, 0, 0, tzinfo=UTC),
        execute_at=datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC),
        commands=_single_command(),
    )
    assert plan.execute_at is not None


def test_plan_accepts_omitted_optional_timestamps() -> None:
    plan = Plan(id="plan-omitted-timestamps", commands=_single_command())
    assert plan.execute_at is None
    assert plan.expires_at is None


def test_command_accepts_preconditions_within_the_limit() -> None:
    Command(
        id="command-preconditions-limit",
        device_id="living_room.main_light",
        command="set_brightness",
        value=50,
        idempotency_key="intent-preconditions-limit",
        preconditions=[
            Precondition(device_id="living_room.main_light", capability="power", expected=True)
            for _ in range(16)
        ],
    )


def test_command_rejects_preconditions_beyond_the_limit() -> None:
    with pytest.raises(ValidationError):
        Command(
            id="command-preconditions-over-limit",
            device_id="living_room.main_light",
            command="set_brightness",
            value=50,
            idempotency_key="intent-preconditions-over-limit",
            preconditions=[
                Precondition(
                    device_id="living_room.main_light", capability="power", expected=True
                )
                for _ in range(17)
            ],
        )
