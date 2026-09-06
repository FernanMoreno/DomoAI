"""Bounded, non-executable codecs for declared MQTT payloads."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence

from domoai.domain.models import CapabilityKind, ScalarValue


def decode_json_scalar(payload: bytes) -> ScalarValue:
    """Decode one JSON scalar; mappings never execute arbitrary payload logic."""

    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("MQTT payload is not valid JSON") from error
    if not isinstance(value, (str, int, float, bool)) or value is None:
        raise ValueError("MQTT payload must decode to a scalar")
    return value


def validate_scalar_value(
    value: ScalarValue | None,
    *,
    kind: CapabilityKind,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
    enum_values: Sequence[str] = (),
) -> ScalarValue:
    """Validate one decoded value against the server-owned mapping contract."""

    if value is None:
        raise ValueError("MQTT value is required")
    if kind is CapabilityKind.BOOLEAN and not isinstance(value, bool):
        raise ValueError("MQTT value must be boolean")
    if kind is CapabilityKind.INTEGER and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise ValueError("MQTT value must be integer")
    if kind is CapabilityKind.NUMBER and (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError("MQTT value must be number")
    text_kinds = {CapabilityKind.ENUM, CapabilityKind.TEXT, CapabilityKind.TIMESTAMP}
    if kind in text_kinds and not isinstance(value, str):
        raise ValueError("MQTT value must be text")
    if kind is CapabilityKind.ENUM and value not in enum_values:
        raise ValueError("MQTT value is not one of enum_values")
    if minimum is not None and value < minimum:  # type: ignore[operator]
        raise ValueError("MQTT value is below minimum")
    if maximum is not None and value > maximum:  # type: ignore[operator]
        raise ValueError("MQTT value is above maximum")
    return value


def decode_json_value(
    payload: bytes,
    *,
    kind: CapabilityKind,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
    enum_values: Sequence[str] = (),
) -> ScalarValue:
    return validate_scalar_value(
        decode_json_scalar(payload),
        kind=kind,
        minimum=minimum,
        maximum=maximum,
        enum_values=enum_values,
    )
