"""Strict, credential-free configuration for Home Assistant Provider mappings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from domoai.domain.models import StrictModel


class HomeAssistantMappingConfigurationError(ValueError):
    """Raised when a local Home Assistant mapping document is not safe to use."""


class HomeAssistantMappingDocument(StrictModel):
    schema_version: Literal["v1"]
    metric_mappings: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mappings(self) -> HomeAssistantMappingDocument:
        for entity_id, mapping in self.metric_mappings.items():
            if not entity_id.strip() or not mapping:
                raise ValueError("metric mappings require a Home Assistant entity and entries")
            for capability, metric in mapping.items():
                if not capability.strip() or not metric.strip():
                    raise ValueError("metric mapping keys and values must be non-empty")
        return self


def load_metric_mappings(path: Path) -> dict[str, dict[str, str]]:
    """Load one strict local mapping document without exposing parse details."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        document = HomeAssistantMappingDocument.model_validate(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ) as error:
        raise HomeAssistantMappingConfigurationError(
            "Home Assistant mapping file is unavailable or not valid v1 JSON"
        ) from error
    return document.metric_mappings


__all__ = [
    "HomeAssistantMappingConfigurationError",
    "HomeAssistantMappingDocument",
    "load_metric_mappings",
]
