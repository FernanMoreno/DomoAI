import pytest

from domoai.adapters.mqtt.codec import decode_json_scalar, decode_json_value, validate_scalar_value
from domoai.domain.models import CapabilityKind


def test_decode_json_scalar_accepts_a_boolean_payload() -> None:
    assert decode_json_scalar(b"true") is True


def test_decode_json_scalar_rejects_a_collection_payload() -> None:
    with pytest.raises(ValueError, match="scalar"):
        decode_json_scalar(b'{"power": true}')


def test_decode_json_value_rejects_a_boolean_for_a_number_capability() -> None:
    with pytest.raises(ValueError, match="number"):
        decode_json_value(b"true", kind=CapabilityKind.NUMBER)


def test_decode_json_value_rejects_a_value_outside_declared_range() -> None:
    with pytest.raises(ValueError, match="maximum"):
        decode_json_value(
            b"31",
            kind=CapabilityKind.NUMBER,
            minimum=16,
            maximum=30,
        )


def test_validate_scalar_value_accepts_declared_enum_value() -> None:
    assert (
        validate_scalar_value(
            "heat",
            kind=CapabilityKind.ENUM,
            enum_values=("off", "heat"),
        )
        == "heat"
    )
