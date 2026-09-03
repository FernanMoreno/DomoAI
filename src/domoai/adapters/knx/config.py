"""Strict, adapter-local KNX mapping configuration."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel

KnxSemanticType = Literal["light", "switch", "sensor", "energy"]
KnxCapabilityName = Literal[
    "power",
    "brightness",
    "temperature",
    "humidity",
    "occupancy",
    "battery.soc",
    "battery.power",
    "battery.capacity",
]

CAPABILITY_DPTS: dict[str, str] = {
    "power": "1.001",
    "brightness": "5.001",
    "temperature": "9.001",
    "humidity": "9.007",
    "occupancy": "1.018",
    "battery.soc": "13.013",
    "battery.power": "9.024",
    "battery.capacity": "13.013",
}

_GROUP_ADDRESS_PATTERN = re.compile(r"^(\d+)/(\d+)(?:/(\d+))?$")
_SAFE_ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]*$"


class KnxCapabilityBinding(StrictModel):
    name: KnxCapabilityName
    dpt: str = Field(min_length=1)
    state_group_address: str = Field(min_length=3)
    command_group_address: str | None = Field(default=None, min_length=3)

    @model_validator(mode="after")
    def validate_binding(self) -> KnxCapabilityBinding:
        expected_dpt = CAPABILITY_DPTS[self.name]
        if self.dpt != expected_dpt:
            raise ValueError(f"{self.name} requires DPT {expected_dpt}")
        _validate_group_address(self.state_group_address)
        if self.command_group_address is not None:
            _validate_group_address(self.command_group_address)
            if self.name not in {"power", "brightness", "battery.power"}:
                raise ValueError(f"{self.name} is read-only and cannot have a command address")
        return self


class KnxEntityConfig(StrictModel):
    entity_id: str = Field(min_length=1, pattern=_SAFE_ID_PATTERN)
    name: str = Field(min_length=1)
    area_id: str | None = Field(default=None, min_length=1, pattern=_SAFE_ID_PATTERN)
    semantic_type: KnxSemanticType
    manufacturer: str | None = None
    model: str | None = None
    capabilities: list[KnxCapabilityBinding] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_profile(self) -> KnxEntityConfig:
        names = [capability.name for capability in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate capability in entity {self.entity_id}")
        allowed = {
            "light": {"power", "brightness"},
            "switch": {"power"},
            "sensor": {"temperature", "humidity", "occupancy"},
            "energy": {"battery.soc", "battery.power", "battery.capacity"},
        }[self.semantic_type]
        unsupported = set(names) - allowed
        if unsupported:
            raise ValueError(
                f"entity {self.entity_id} has unsupported capabilities: {sorted(unsupported)}"
            )
        if self.semantic_type == "sensor" and any(
            capability.command_group_address is not None for capability in self.capabilities
        ):
            raise ValueError(f"sensor entity {self.entity_id} cannot be writable")
        return self


class KnxMappingDocument(StrictModel):
    schema_version: Literal["v1"]
    entities: list[KnxEntityConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_document(self) -> KnxMappingDocument:
        entity_ids = [entity.entity_id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("duplicate KNX entity_id")
        canonical_ids = [canonical_device_id(entity) for entity in self.entities]
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("duplicate derived canonical KNX identity")
        dpts_by_address: dict[str, str] = {}
        for entity in self.entities:
            for capability in entity.capabilities:
                previous = dpts_by_address.setdefault(
                    capability.state_group_address, capability.dpt
                )
                if previous != capability.dpt:
                    raise ValueError(
                        "shared KNX state address "
                        f"{capability.state_group_address} has multiple DPTs"
                    )
        return self


def canonical_device_id(entity: KnxEntityConfig) -> str:
    area = entity.area_id or "unassigned"
    return f"{area}.{_slug(entity.name)}"


def load_mapping(path: Path) -> KnxMappingDocument:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("KNX mapping file was not found") from error
    except OSError as error:
        raise ValueError("KNX mapping file could not be read") from error
    except json.JSONDecodeError as error:
        raise ValueError("KNX mapping file is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("KNX mapping document must be a JSON object")
    return KnxMappingDocument.model_validate(payload)


def _validate_group_address(value: str) -> None:
    match = _GROUP_ADDRESS_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid KNX group address: {value}")
    parts = [int(part) for part in match.groups() if part is not None]
    if len(parts) == 3:
        if not 0 <= parts[0] <= 15 or not 0 <= parts[1] <= 15 or not 0 <= parts[2] <= 255:
            raise ValueError(f"KNX three-level group address is out of range: {value}")
    elif not 0 <= parts[0] <= 31 or not 0 <= parts[1] <= 2047:
        raise ValueError(f"KNX two-level group address is out of range: {value}")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "device"
