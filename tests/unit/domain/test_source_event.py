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


def test_state_event_decodes_legacy_semantic_fields() -> None:
    event = StateChangedEvent(
        payload={
            "source_adapter_id": "zigbee2mqtt",
            "friendly_name": "lamp",
            "capability": "brightness",
            "value": 42,
            "unit": "%",
            "available": True,
        }
    )
    assert event.source_adapter_id == "zigbee2mqtt"
    assert event.external_id == "lamp"
    assert event.capability == "brightness"
    assert event.value == 42
    assert event.unit == "%"
    assert event.available is True


@pytest.mark.parametrize(
    ("cls", "payload", "expected"),
    [
        (AvailabilityChangedEvent, {"friendly_name": "lamp", "available": False}, "lamp"),
        (DeviceMembershipChangedEvent, {"node_id": "node-1"}, "node-1"),
        (MetadataChangedEvent, {"entity_id": "light.one", "friendly_name": "One"}, "light.one"),
        (AdapterDiagnosticEvent, {"reason": "timeout"}, "timeout"),
    ],
)
def test_structural_events_decode_variant_fields(cls: type, payload: dict, expected: str) -> None:
    event = cls(payload=payload)
    if cls is AdapterDiagnosticEvent:
        assert event.message == expected
    else:
        assert event.external_id == expected


def test_literal_mismatch_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StateChangedEvent(kind="availability_changed")


@pytest.mark.parametrize("cls", _CLASSES)
def test_unrecognized_kind_is_rejected(cls: type) -> None:
    with pytest.raises(ValidationError):
        cls(kind="totally_unknown_kind")
