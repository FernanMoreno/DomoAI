import pytest

from domoai.domain.errors import InvalidTransitionError
from domoai.domain.models import AvailabilityStatus, PlanStatus
from domoai.domain.transitions import assert_availability_transition, assert_plan_transition


def test_plan_can_move_from_draft_to_validated() -> None:
    assert_plan_transition(PlanStatus.DRAFT, PlanStatus.VALIDATED)


def test_plan_cannot_move_from_draft_to_executing() -> None:
    with pytest.raises(InvalidTransitionError):
        assert_plan_transition(PlanStatus.DRAFT, PlanStatus.EXECUTING)


def test_unavailable_device_can_recover_to_available() -> None:
    assert_availability_transition(AvailabilityStatus.UNAVAILABLE, AvailabilityStatus.AVAILABLE)


def test_current_device_cannot_become_unknown_without_refresh() -> None:
    with pytest.raises(InvalidTransitionError):
        assert_availability_transition(AvailabilityStatus.AVAILABLE, AvailabilityStatus.UNKNOWN)
