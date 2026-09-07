"""Strict loading of server-owned EV charging bindings."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from domoai.domain.energy import EVChargingBinding


class EVChargingProfileConfigurationError(ValueError):
    """Raised when an EV charging profile cannot authorize a safe route."""


def load_ev_charging_bindings(path: Path) -> tuple[EVChargingBinding, ...]:
    """Load the explicit, provider/state-bound EV actuator bindings."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("bindings"), list):
            raise ValueError("EV charging profile must contain a bindings list")
        bindings = tuple(EVChargingBinding.model_validate(item) for item in payload["bindings"])
        device_ids = [binding.device_id for binding in bindings]
        if len(device_ids) != len(set(device_ids)):
            raise ValueError("EV charging profile contains duplicate devices")
        return bindings
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise EVChargingProfileConfigurationError(
            "EV charging profile is unavailable or not valid v1 JSON"
        ) from error


__all__ = ["EVChargingProfileConfigurationError", "load_ev_charging_bindings"]
