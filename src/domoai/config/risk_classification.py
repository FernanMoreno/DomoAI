"""Load operator risk-classification override rules."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from domoai.domain.models import RiskClass
from domoai.runtime.risk_classifier import RiskOverride


def load_risk_overrides(payload: Mapping[str, Any] | list[Mapping[str, Any]]) -> list[RiskOverride]:
    raw_overrides = payload if isinstance(payload, list) else payload.get("overrides", [])
    return [
        RiskOverride(
            device_id=raw.get("device_id"),
            area_id=raw.get("area_id"),
            risk_class=RiskClass(raw["risk_class"]),
        )
        for raw in raw_overrides
    ]


def load_risk_overrides_file(path: Path) -> list[RiskOverride]:
    with path.open("rb") as file:
        return load_risk_overrides(tomllib.load(file))
