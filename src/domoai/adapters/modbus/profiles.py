"""Versioned, deterministic Modbus profile contracts."""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import Field, model_validator

from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.domain.models import StrictModel


class ModbusProfile(StrictModel):
    profile_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    profile_version: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+(?:\.[0-9]+){0,2}$")
    vendor_name: str = Field(min_length=1, max_length=128)
    product_code: str = Field(min_length=1, max_length=128)
    min_firmware: str | None = Field(default=None, max_length=32)
    max_firmware: str | None = Field(default=None, max_length=32)
    mapping: ModbusMappingDocument
    provenance: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_constraints(self) -> ModbusProfile:
        if self.min_firmware is not None and _version(self.min_firmware) is None:
            raise ValueError("min_firmware must be numeric dotted version")
        if self.max_firmware is not None and _version(self.max_firmware) is None:
            raise ValueError("max_firmware must be numeric dotted version")
        if self.min_firmware is not None and self.max_firmware is not None:
            lower = _version(self.min_firmware)
            upper = _version(self.max_firmware)
            assert lower is not None and upper is not None
            if lower > upper:
                raise ValueError("min_firmware must not exceed max_firmware")
        return self

    @property
    def signature(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    def supports_firmware(self, revision: str) -> bool:
        current = _version(revision)
        if current is None:
            return False
        if self.min_firmware is not None:
            lower = _version(self.min_firmware)
            assert lower is not None
            if current < lower:
                return False
        if self.max_firmware is not None:
            upper = _version(self.max_firmware)
            assert upper is not None
            if current > upper:
                return False
        return True


def _version(value: str) -> tuple[int, ...] | None:
    if not isinstance(value, str) or re.fullmatch(r"\d+(?:\.\d+){0,2}", value) is None:
        return None
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (3 - len(parts))


__all__ = ["ModbusProfile"]
