"""Provider-facing contracts built on top of the canonical domain model."""

from __future__ import annotations

import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .models import (
    Capability,
    DeviceType,
    ExecutionStatus,
    ScalarValue,
    SourceRef,
    StrictModel,
)

PROVIDER_CONTRACT_VERSION: Literal["v1"] = "v1"
PROVIDER_SCHEMA_VERSION: Literal["v1"] = "v1"
_IDENTIFIER = r"^[a-z0-9][a-z0-9_.-]*$"
_SENSITIVE_FRAGMENTS = (
    "token",
    "password",
    "secret",
    "authorization",
    "credential",
    "api_key",
    "cookie",
)


class ProviderRole(StrEnum):
    TELEMETRY = "telemetry"
    COMMANDS = "commands"


class MeasurementQuality(StrEnum):
    GOOD = "good"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


def _contains_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS)


def _validate_unique(values: list[str], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must be unique")


class ProviderManifest(StrictModel):
    """Stable, non-secret declaration of a provider package."""

    schema_version: Literal["v1"] = PROVIDER_SCHEMA_VERSION
    contract_version: Literal["v1"] = PROVIDER_CONTRACT_VERSION
    provider_id: str = Field(min_length=1, pattern=_IDENTIFIER)
    name: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    package_name: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    roles: list[ProviderRole] = Field(min_length=1)
    device_types: list[DeviceType] = Field(min_length=1)
    capabilities: list[Capability] = Field(min_length=1)
    metadata: dict[str, ScalarValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_declarations(self) -> ProviderManifest:
        _validate_unique([role.value for role in self.roles], "roles")
        _validate_unique([device_type.value for device_type in self.device_types], "device_types")
        _validate_unique([capability.name for capability in self.capabilities], "capability names")
        for capability in self.capabilities:
            if not re.fullmatch(_IDENTIFIER, capability.name):
                raise ValueError(f"invalid canonical capability name {capability.name!r}")
            _validate_unique(capability.commands, f"commands for {capability.name}")
            if capability.writable and not capability.commands:
                raise ValueError(f"writable capability {capability.name!r} requires commands")
            if not capability.writable and capability.commands:
                raise ValueError(
                    f"read-only capability {capability.name!r} cannot declare commands"
                )
        for key in self.metadata:
            if _contains_sensitive_key(key):
                raise ValueError(f"metadata contains sensitive field {key!r}")
        return self


class DeviceDescriptor(StrictModel):
    """Provider-local identity before canonical cross-provider merging."""

    schema_version: Literal["v1"] = PROVIDER_SCHEMA_VERSION
    provider_id: str = Field(min_length=1, pattern=_IDENTIFIER)
    external_id: str = Field(min_length=1, max_length=256)
    device_type: DeviceType
    name: str = Field(min_length=1, max_length=256)
    manufacturer: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, max_length=256)
    serial_number: str | None = Field(default=None, max_length=256)
    area_id: str | None = Field(default=None, max_length=256)
    capabilities: list[Capability] = Field(default_factory=list)
    identity_keys: list[str] = Field(default_factory=list)
    connections: list[str] = Field(default_factory=list)
    parent_external_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_identity(self) -> DeviceDescriptor:
        _validate_unique(self.identity_keys, "identity_keys")
        _validate_unique(self.connections, "connections")
        _validate_unique([capability.name for capability in self.capabilities], "capability names")
        return self


class Measurement(StrictModel):
    """Normalized observation with explicit provider provenance."""

    schema_version: Literal["v1"] = PROVIDER_SCHEMA_VERSION
    provider_id: str = Field(min_length=1, pattern=_IDENTIFIER)
    device_id: str = Field(min_length=1, max_length=256)
    metric: str = Field(min_length=1, pattern=_IDENTIFIER)
    value: ScalarValue
    unit: str | None = Field(default=None, max_length=64)
    observed_at: datetime
    received_at: datetime
    quality: MeasurementQuality = MeasurementQuality.GOOD
    source_ref: SourceRef

    @model_validator(mode="after")
    def validate_observation(self) -> Measurement:
        if self.observed_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("observed_at and received_at must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("received_at must be greater than or equal to observed_at")
        if self.source_ref.adapter_id != self.provider_id:
            raise ValueError("source_ref.adapter_id must match provider_id")
        if isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            if not math.isfinite(self.value):
                raise ValueError("numeric measurement values must be finite")
        return self


class ProviderCommand(StrictModel):
    """Provider-local command after runtime policy and plan validation."""

    schema_version: Literal["v1"] = PROVIDER_SCHEMA_VERSION
    provider_id: str = Field(min_length=1, pattern=_IDENTIFIER)
    external_device_id: str = Field(min_length=1, max_length=256)
    command: str = Field(min_length=1, pattern=_IDENTIFIER)
    params: dict[str, ScalarValue] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=256)
    intent: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_parameters(self) -> ProviderCommand:
        unsafe = [key for key in self.params if _contains_sensitive_key(key)]
        if unsafe:
            raise ValueError(f"command parameters contain sensitive fields: {unsafe!r}")
        return self


class ProviderExecutionResult(StrictModel):
    """Safe outcome returned by a command-capable provider."""

    schema_version: Literal["v1"] = PROVIDER_SCHEMA_VERSION
    provider_id: str = Field(min_length=1, pattern=_IDENTIFIER)
    external_device_id: str = Field(min_length=1, max_length=256)
    command: str = Field(min_length=1, pattern=_IDENTIFIER)
    status: ExecutionStatus
    completed_at: datetime
    message: str | None = Field(default=None, max_length=240)
    source_ref: SourceRef | None = None

    @model_validator(mode="after")
    def validate_result(self) -> ProviderExecutionResult:
        if self.completed_at.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware")
        if self.source_ref is not None and self.source_ref.adapter_id != self.provider_id:
            raise ValueError("source_ref.adapter_id must match provider_id")
        return self


class ProviderDiagnostic(StrictModel):
    """Stable diagnostic that cannot leak provider exception payloads."""

    schema_version: Literal["v1"] = PROVIDER_SCHEMA_VERSION
    code: str = Field(min_length=1, pattern=_IDENTIFIER)
    provider_id: str = Field(min_length=1, pattern=_IDENTIFIER)
    message: str = Field(min_length=1, max_length=240)
    retryable: bool = False
    details: dict[str, ScalarValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_details(self) -> ProviderDiagnostic:
        unsafe = [key for key in self.details if _contains_sensitive_key(key)]
        if unsafe:
            raise ValueError(f"diagnostic details contain sensitive fields: {unsafe!r}")
        return self


class ProviderDiscoveryResult(StrictModel):
    schema_version: Literal["v1"] = PROVIDER_SCHEMA_VERSION
    devices: list[DeviceDescriptor] = Field(default_factory=list)
    diagnostics: list[ProviderDiagnostic] = Field(default_factory=list)


class ProviderCollectionResult(StrictModel):
    schema_version: Literal["v1"] = PROVIDER_SCHEMA_VERSION
    measurements: list[Measurement] = Field(default_factory=list)
    diagnostics: list[ProviderDiagnostic] = Field(default_factory=list)
