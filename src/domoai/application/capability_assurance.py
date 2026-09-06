"""Validation of capability guarantees at the command boundary."""

from domoai.domain.errors import ErrorCode
from domoai.domain.models import Capability, Command, ErrorDetail


def validate_command_assurance(
    command: Command, capability: Capability
) -> list[ErrorDetail]:
    """Require a verifiable postcondition for capabilities that demand readback."""

    guarantees = capability.guarantees
    if not guarantees.readback_required:
        return []
    if any(
        postcondition.capability == capability.name
        and postcondition.verification != "unconfirmed"
        for postcondition in command.postconditions
    ):
        return []
    return [
        ErrorDetail(
            code=ErrorCode.INVALID_CAPABILITY,
            message=(
                f"Capability {capability.name!r} requires a verifiable readback "
                "postcondition"
            ),
            field="postconditions",
            device_id=command.device_id,
            capability=capability.name,
            details={"readback_required": True},
        )
    ]
