import pytest
from pydantic import ValidationError

from domoai.domain.capabilities import (
    AvailabilityMode,
    CapabilityGuarantees,
    CommissioningRequirement,
)
from domoai.domain.models import Capability, CapabilityKind


def test_legacy_capability_uses_safe_defaults_without_new_payload_fields() -> None:
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

    assert capability.guarantees.readback_required is False
    assert capability.guarantees.availability_modes == [AvailabilityMode.LOCAL]
    assert "guarantees" not in capability.model_dump(exclude_defaults=True)


def test_numeric_guarantees_round_trip_and_are_visible_when_declared() -> None:
    capability = Capability(
        name="target_temperature",
        kind=CapabilityKind.NUMBER,
        unit="degC",
        readable=True,
        writable=True,
        minimum=16,
        maximum=30,
        commands=["set_temperature"],
        guarantees=CapabilityGuarantees(
            resolution=0.5,
            tolerance=0.5,
            expected_latency_ms=750,
            readback_required=True,
            availability_modes=[AvailabilityMode.LOCAL, AvailabilityMode.REMOTE],
            commissioning_required=CommissioningRequirement.RECOMMENDED,
        ),
    )

    payload = capability.model_dump(mode="json")
    restored = Capability.model_validate(payload)

    assert restored == capability
    assert payload["guarantees"]["resolution"] == 0.5
    assert payload["guarantees"]["readback_required"] is True


@pytest.mark.parametrize(
    "guarantees, message",
    [
        ({"resolution": 0}, "resolution"),
        ({"tolerance": -1}, "tolerance"),
        ({"expected_latency_ms": -1}, "expected_latency_ms"),
        ({"availability_modes": [AvailabilityMode.LOCAL, AvailabilityMode.LOCAL]}, "unique"),
    ],
)
def test_invalid_guarantee_values_are_rejected(
    guarantees: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CapabilityGuarantees.model_validate(guarantees)


def test_readback_and_confirmation_require_a_writable_readable_capability() -> None:
    with pytest.raises(ValidationError, match="readback"):
        Capability(
            name="temperature",
            kind=CapabilityKind.NUMBER,
            unit="degC",
            readable=False,
            writable=True,
            commands=["set_temperature"],
            guarantees=CapabilityGuarantees(readback_required=True),
        )

    with pytest.raises(ValidationError, match="confirmation"):
        Capability(
            name="temperature",
            kind=CapabilityKind.NUMBER,
            unit="degC",
            readable=True,
            writable=False,
            guarantees=CapabilityGuarantees(confirmation_required=True),
        )
