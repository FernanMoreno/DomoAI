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
