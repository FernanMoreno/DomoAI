"""Strict, adapter-local Modbus mapping configuration."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel

ModbusArea = Literal["coil", "discrete_input", "input_register", "holding_register"]
ModbusDataType = Literal["bool", "uint16", "int16", "float32"]
ByteOrder = Literal["big", "little"]
ModbusSemanticType = Literal["light", "switch", "sensor", "energy"]
ModbusCapabilityName = Literal[
    "power",
    "brightness",
    "temperature",
    "humidity",
    "occupancy",
    "battery.soc",
    "battery.power",
    "battery.capacity",
    "ev.soc",
    "ev_charging",
    "ev.capacity",
    "ev.connected",
    "water.flow_rate",
    "water.total_volume",
    "thermal.indoor_temperature",
    "thermal.hvac_power",
]

_SAFE_ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]*$"


class ModbusPoint(StrictModel):
    """One explicit Modbus state or command address."""

    area: ModbusArea
    address: int = Field(strict=True, ge=0, le=65535)
    data_type: ModbusDataType
    byte_order: ByteOrder = "big"
    word_order: ByteOrder = "big"
    scale: float = Field(default=1.0)
    offset: float = Field(default=0.0)

    @property
    def register_count(self) -> int:
        return 1 if self.data_type != "float32" else 2

    @model_validator(mode="after")
    def validate_point(self) -> ModbusPoint:
        if not math.isfinite(self.scale) or self.scale == 0:
            raise ValueError("Modbus scale must be finite and non-zero")
        if not math.isfinite(self.offset):
            raise ValueError("Modbus offset must be finite")
        if self.address + self.register_count > 65536:
            raise ValueError("Modbus point exceeds the 16-bit address space")
        if self.data_type == "bool":
            if self.area not in {"coil", "discrete_input"}:
                raise ValueError("boolean Modbus points require coil or discrete_input")
            if self.scale != 1 or self.offset != 0:
                raise ValueError("boolean Modbus points cannot use numeric conversion")
        elif self.area in {"coil", "discrete_input"}:
            raise ValueError("numeric Modbus points require a register area")
        return self


class ModbusCapabilityBinding(StrictModel):
    name: ModbusCapabilityName
    state: ModbusPoint
    command: ModbusPoint | None = None

    @model_validator(mode="after")
    def validate_profile(self) -> ModbusCapabilityBinding:
        state = self.state
        if self.name == "power":
            if state.data_type != "bool" or state.area not in {"coil", "discrete_input"}:
                raise ValueError("power state requires a boolean coil or discrete input")
            if self.command is not None and (
                self.command.data_type != "bool" or self.command.area != "coil"
            ):
                raise ValueError("writable power requires a boolean coil command")
        elif self.name == "brightness":
            if state.area != "holding_register" or state.data_type == "bool":
                raise ValueError("brightness state requires a numeric holding register")
            if self.command is not None and (
                self.command.area != "holding_register" or self.command.data_type == "bool"
            ):
                raise ValueError("brightness command requires a numeric holding register")
        elif self.name in {
            "temperature",
            "humidity",
            "battery.soc",
            "battery.power",
            "battery.capacity",
            "ev.soc",
            "ev_charging",
            "ev.capacity",
            "water.flow_rate",
            "water.total_volume",
            "thermal.indoor_temperature",
            "thermal.hvac_power",
        }:
            if state.area not in {"input_register", "holding_register"}:
                raise ValueError(f"{self.name} requires an input or holding register")
            if state.data_type == "bool":
                raise ValueError(f"{self.name} is read-only and numeric")
            if self.command is not None:
                if self.name not in {"battery.power", "ev_charging", "thermal.hvac_power"}:
                    raise ValueError(f"{self.name} is read-only and numeric")
                if self.command.area != "holding_register" or self.command.data_type == "bool":
                    raise ValueError(f"{self.name} command requires a numeric holding register")
        elif self.name in {"occupancy", "ev.connected"}:
            if state.data_type != "bool" or state.area not in {"coil", "discrete_input"}:
                raise ValueError(f"{self.name} requires a boolean coil or discrete input")
            if self.command is not None:
                raise ValueError(f"{self.name} is read-only")
        return self


class ModbusEntityConfig(StrictModel):
    entity_id: str = Field(min_length=1, pattern=_SAFE_ID_PATTERN)
    name: str = Field(min_length=1)
    area_id: str | None = Field(default=None, min_length=1, pattern=_SAFE_ID_PATTERN)
    semantic_type: ModbusSemanticType
    manufacturer: str | None = None
    model: str | None = None
    unit_id: int = Field(strict=True, ge=1, le=247)
    capabilities: list[ModbusCapabilityBinding] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_profile(self) -> ModbusEntityConfig:
        names = [capability.name for capability in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate Modbus capability in entity {self.entity_id}")
        allowed = {
            "light": {"power", "brightness"},
            "switch": {"power"},
            "sensor": {"temperature", "humidity", "occupancy"},
            "energy": {
                "battery.soc",
                "battery.power",
                "battery.capacity",
                "ev.soc",
                "ev_charging",
                "ev.capacity",
                "ev.connected",
                "water.flow_rate",
                "water.total_volume",
                "thermal.indoor_temperature",
                "thermal.hvac_power",
            },
        }[self.semantic_type]
        unsupported = set(names) - allowed
        if unsupported:
            raise ValueError(
                f"entity {self.entity_id} has unsupported capabilities: {sorted(unsupported)}"
            )
        if self.semantic_type == "sensor" and any(
            capability.command is not None for capability in self.capabilities
        ):
            raise ValueError(f"sensor entity {self.entity_id} cannot be writable")
        return self


class ModbusMappingDocument(StrictModel):
    schema_version: Literal["v1"]
    entities: list[ModbusEntityConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_document(self) -> ModbusMappingDocument:
        entity_ids = [entity.entity_id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("duplicate Modbus entity_id")
        canonical_ids = [canonical_device_id(entity) for entity in self.entities]
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("duplicate derived canonical Modbus identity")
        points: dict[tuple[int, str, int], tuple[object, ...]] = {}
        for entity in self.entities:
            for capability in entity.capabilities:
                point = capability.state
                key = (entity.unit_id, point.area, point.address)
                signature = (
                    point.data_type,
                    point.register_count,
                    point.byte_order,
                    point.word_order,
                    point.scale,
                    point.offset,
                )
                previous = points.setdefault(key, signature)
                if previous != signature:
                    raise ValueError("shared Modbus state point has conflicting codec parameters")
        return self


def canonical_device_id(entity: ModbusEntityConfig) -> str:
    area = entity.area_id or "unassigned"
    return f"{area}.{_slug(entity.name)}"


def load_mapping(path: Path) -> ModbusMappingDocument:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("Modbus mapping file was not found") from error
    except OSError as error:
        raise ValueError("Modbus mapping file could not be read") from error
    except json.JSONDecodeError as error:
        raise ValueError("Modbus mapping file is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("Modbus mapping document must be a JSON object")
    return ModbusMappingDocument.model_validate(payload)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "device"
