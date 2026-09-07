from domoai.application.capability_assurance import validate_command_assurance
from domoai.domain.capabilities import CapabilityGuarantees
from domoai.domain.models import Capability, CapabilityKind, Command


def _command(*, postconditions: list[dict] | None = None) -> Command:
    return Command(
        id="command-1",
        device_id="climate.living_room",
        command="set_temperature",
        value=21,
        unit="degC",
        idempotency_key="idem-1",
        postconditions=postconditions or [],
    )


def test_readback_required_rejects_command_without_postcondition() -> None:
    capability = Capability(
        name="target_temperature",
        kind=CapabilityKind.NUMBER,
        unit="degC",
        readable=True,
        writable=True,
        commands=["set_temperature"],
        guarantees=CapabilityGuarantees(readback_required=True),
    )

    errors = validate_command_assurance(_command(), capability)

    assert [error.code for error in errors] == ["invalid_capability"]
    assert errors[0].field == "postconditions"


def test_readback_required_allows_a_postcondition_for_the_capability() -> None:
    capability = Capability(
        name="target_temperature",
        kind=CapabilityKind.NUMBER,
        unit="degC",
        readable=True,
        writable=True,
        commands=["set_temperature"],
        guarantees=CapabilityGuarantees(readback_required=True),
    )

    errors = validate_command_assurance(
        _command(
            postconditions=[
                {
                    "capability": "target_temperature",
                    "expected": 21,
                }
            ]
        ),
        capability,
    )

    assert errors == []
