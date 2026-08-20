from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.models import (
    AdapterDiagnosticEvent,
    AvailabilityChangedEvent,
    DeviceMembershipChangedEvent,
    MetadataChangedEvent,
    StateChangedEvent,
)

_CLASSES = [
    StateChangedEvent,
    AvailabilityChangedEvent,
    DeviceMembershipChangedEvent,
    MetadataChangedEvent,
    AdapterDiagnosticEvent,
]


@pytest.mark.parametrize("cls", _CLASSES)
def test_each_variant_constructs_with_its_default_kind(cls: type) -> None:
    event = cls(payload={"x": 1})
    assert event.kind == cls.model_fields["kind"].default
    assert event.payload == {"x": 1}


@pytest.mark.parametrize("cls", _CLASSES)
def test_each_variant_defaults_payload_to_empty_dict(cls: type) -> None:
    event = cls()
    assert event.payload == {}


def test_literal_mismatch_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StateChangedEvent(kind="availability_changed")


@pytest.mark.parametrize("cls", _CLASSES)
def test_unrecognized_kind_is_rejected(cls: type) -> None:
    with pytest.raises(ValidationError):
        cls(kind="totally_unknown_kind")
