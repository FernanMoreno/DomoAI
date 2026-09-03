"""Strict, credential-free configuration for EV charging bindings."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from domoai.domain.energy import EVChargingBinding


class EVChargingProfileConfigurationError(ValueError):
    """Raised when a configured EV charging binding is unsafe."""


def load_ev_charging_binding(path: Path) -> EVChargingBinding:
    """Load one complete server-owned canonical EV charging binding."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EVChargingBinding.model_validate(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise EVChargingProfileConfigurationError(
            "EV charging binding is unavailable or not valid v1 JSON"
        ) from error


__all__ = [
    "EVChargingProfileConfigurationError",
    "load_ev_charging_binding",
]
