"""Typed, confirmation-aware connection repair contracts."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import ScalarValue, StrictModel

_DIGEST = r"^[0-9a-f]{64}$"
_SENSITIVE = re.compile(
    r"(?:token|password|passwd|secret|bearer|authorization|api[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)


class RepairActionName(StrEnum):
    REAUTHENTICATE = "reauthenticate"
    RETRY = "retry"
    SELECT_INTERFACE = "select_interface"
    REPLACE_ENDPOINT = "replace_endpoint"
    IMPORT_MAPPING = "import_mapping"
    SELECT_PROFILE = "select_profile"
    TRUST_CERTIFICATE = "trust_certificate"
    UPDATE_CREDENTIALS = "update_credentials"


class RepairRisk(StrEnum):
    SAFE = "safe"
    CONFIGURATION = "configuration"
    SECURITY = "security"


type RepairParameters = dict[str, ScalarValue]


class RepairAction(StrictModel):
    action_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    action: RepairActionName
    parameters: RepairParameters = Field(default_factory=dict)
    requires_confirmation: bool
    risk: RepairRisk
    diagnostic_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    plan_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_parameters(self) -> RepairAction:
        if any(_SENSITIVE.search(key) for key in self.parameters):
            raise ValueError("repair parameters cannot contain sensitive fields")
        if any(
            _SENSITIVE.search(value)
            for value in self.parameters.values()
            if isinstance(value, str)
        ):
            raise ValueError("repair parameters cannot contain sensitive material")
        if self.action is RepairActionName.RETRY:
            if self.requires_confirmation or self.risk is not RepairRisk.SAFE:
                raise ValueError("retry must be a safe non-confirmed action")
        elif not self.requires_confirmation:
            raise ValueError("configuration and security repairs require confirmation")
        return self


class RepairPlan(StrictModel):
    schema_version: Literal["v1"] = "v1"
    connection_id: str = Field(min_length=1, max_length=128)
    diagnostics_digest: str = Field(pattern=_DIGEST)
    actions: tuple[RepairAction, ...] = Field(max_length=32)
    created_at: datetime

    @model_validator(mode="after")
    def validate_plan(self) -> RepairPlan:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("repair plan timestamp must be timezone-aware")
        action_ids = [item.action_id for item in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("repair action IDs must be unique")
        return self


__all__ = ["RepairAction", "RepairActionName", "RepairPlan", "RepairRisk"]
