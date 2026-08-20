"""Load server-owned hard safety limits, structurally separate from policy."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from domoai.domain.models import SafetyLimit


def load_safety_limits(payload: Mapping[str, Any] | list[Mapping[str, Any]]) -> list[SafetyLimit]:
    raw_limits = payload if isinstance(payload, list) else payload.get("limits", [])
    return [SafetyLimit.model_validate(raw) for raw in raw_limits]


def load_safety_limits_file(path: Path) -> list[SafetyLimit]:
    with path.open("rb") as file:
        return load_safety_limits(tomllib.load(file))
