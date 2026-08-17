"""State-transition guards for plans and device availability."""

from domoai.domain.errors import InvalidTransitionError
from domoai.domain.models import AvailabilityStatus, PlanStatus

_PLAN_TRANSITIONS: dict[PlanStatus, set[PlanStatus]] = {
    PlanStatus.DRAFT: {PlanStatus.VALIDATED, PlanStatus.CANCELLED},
    PlanStatus.VALIDATED: {
        PlanStatus.REQUIRES_CONFIRMATION,
        PlanStatus.READY,
        PlanStatus.CANCELLED,
    },
    PlanStatus.REQUIRES_CONFIRMATION: {
        PlanStatus.APPROVED,
        PlanStatus.CANCELLED,
    },
    PlanStatus.READY: {PlanStatus.EXECUTING, PlanStatus.CANCELLED},
    PlanStatus.APPROVED: {PlanStatus.EXECUTING, PlanStatus.CANCELLED},
    PlanStatus.EXECUTING: {
        PlanStatus.COMPLETED,
        PlanStatus.PARTIALLY_FAILED,
        PlanStatus.FAILED,
        PlanStatus.UNKNOWN,
        PlanStatus.CANCELLED,
    },
    PlanStatus.COMPLETED: set(),
    PlanStatus.PARTIALLY_FAILED: set(),
    PlanStatus.FAILED: set(),
    PlanStatus.CANCELLED: set(),
    PlanStatus.UNKNOWN: set(),
}

_AVAILABILITY_TRANSITIONS: dict[AvailabilityStatus, set[AvailabilityStatus]] = {
    AvailabilityStatus.UNKNOWN: {AvailabilityStatus.AVAILABLE, AvailabilityStatus.UNAVAILABLE},
    AvailabilityStatus.AVAILABLE: {AvailabilityStatus.STALE, AvailabilityStatus.UNAVAILABLE},
    AvailabilityStatus.STALE: {AvailabilityStatus.AVAILABLE, AvailabilityStatus.UNAVAILABLE},
    AvailabilityStatus.UNAVAILABLE: {AvailabilityStatus.AVAILABLE},
}


def assert_plan_transition(current: PlanStatus, requested: PlanStatus) -> None:
    if requested not in _PLAN_TRANSITIONS[current]:
        raise InvalidTransitionError(current.value, requested.value)


def assert_availability_transition(
    current: AvailabilityStatus, requested: AvailabilityStatus
) -> None:
    if requested not in _AVAILABILITY_TRANSITIONS[current]:
        raise InvalidTransitionError(current.value, requested.value)
