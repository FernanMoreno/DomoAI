"""Strict server-owned mapping for generic MQTT devices."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import CapabilityKind, StrictModel


class MqttCapabilityMapping(StrictModel):
    name: str = Field(min_length=1, max_length=96)
    kind: CapabilityKind = CapabilityKind.BOOLEAN
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    minimum: float | int | None = None
    maximum: float | int | None = None
    enum_values: list[str] = Field(default_factory=list, max_length=64)
    commands: list[str] = Field(default_factory=list, max_length=16)
    state_topic: str = Field(min_length=1, max_length=256)
    command_topic: str | None = Field(default=None, min_length=1, max_length=256)
    feedback_topic: str | None = Field(default=None, min_length=1, max_length=256)
    require_readback: bool = False

    @model_validator(mode="after")
    def reject_wildcard_write_topic(self) -> MqttCapabilityMapping:
        for topic, label in (
            (self.command_topic, "write"),
            (self.feedback_topic, "feedback"),
        ):
            if topic is not None and any(token in topic for token in ("+", "#")):
                raise ValueError(f"wildcard {label} topics are forbidden")
        if self.require_readback and (self.command_topic is None or self.feedback_topic is None):
            raise ValueError("required readback needs command and feedback topics")
        if len(set(self.commands)) != len(self.commands):
            raise ValueError("commands must contain unique values")
        if self.commands and self.command_topic is None:
            raise ValueError("semantic commands need a command topic")
        for value, label in ((self.minimum, "minimum"), (self.maximum, "maximum")):
            if value is not None and isinstance(value, bool):
                raise ValueError(f"{label} must be numeric")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must be less than or equal to maximum")
        if self.kind not in {CapabilityKind.INTEGER, CapabilityKind.NUMBER} and (
            self.minimum is not None or self.maximum is not None
        ):
            raise ValueError("numeric bounds require an integer or number capability")
        if self.kind is CapabilityKind.ENUM and not self.enum_values:
            raise ValueError("enum capabilities require enum_values")
        if len(set(self.enum_values)) != len(self.enum_values):
            raise ValueError("enum_values must contain unique values")
        return self


class MqttDeviceMapping(StrictModel):
    source_id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64)
    capabilities: list[MqttCapabilityMapping] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def reject_ambiguous_commands(self) -> MqttDeviceMapping:
        commands: list[str] = []
        for capability in self.capabilities:
            if capability.command_topic is None:
                continue
            commands.extend(capability.commands or [capability.name])
        if len(commands) != len(set(commands)):
            raise ValueError("ambiguous writable semantic command")
        return self


class MqttAdapterMapping(StrictModel):
    schema_version: Literal["v1"]
    adapter_id: str = Field(min_length=1, max_length=64)
    devices: list[MqttDeviceMapping] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def reject_duplicate_source_ids(self) -> MqttAdapterMapping:
        source_ids = [device.source_id for device in self.devices]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("duplicate MQTT source_id")
        return self


def load_mapping(path: Path) -> MqttAdapterMapping:
    return MqttAdapterMapping.model_validate_json(path.read_text(encoding="utf-8"))
