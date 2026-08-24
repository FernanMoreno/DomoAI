"""Strict, credential-free configuration for dispatchable battery profiles."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from domoai.domain.energy import DispatchableBatteryBinding


class BatteryProfileConfigurationError(ValueError):
    """Raised when a configured dispatchable battery profile is unsafe."""


def load_dispatchable_battery_binding(path: Path) -> DispatchableBatteryBinding:
    """Load one complete server-owned canonical battery binding."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DispatchableBatteryBinding.model_validate(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise BatteryProfileConfigurationError(
            "dispatchable battery profile is unavailable or not valid v1 JSON"
        ) from error


__all__ = [
    "BatteryProfileConfigurationError",
    "load_dispatchable_battery_binding",
]
