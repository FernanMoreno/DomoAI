"""Versioned contracts for third-party adapter packages."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.models import CapabilityKind, DeviceType, StrictModel

ADAPTER_SDK_SCHEMA_VERSION: Literal["v1"] = "v1"
ADAPTER_CONTRACT_VERSION: Literal["v1"] = "v1"
ADAPTER_ENTRY_POINT_GROUP = "domoai.adapters"

type MetadataValue = bool | int | float | str


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CompatibilityStatus(StrEnum):
    COMPATIBLE = "compatible"
    DEGRADED = "degraded"
    INVALID = "invalid"


class CapabilityCompatibilityStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    OPTIONAL = "optional"
    INVALID = "invalid"


class CapabilityDeclaration(StrictModel):
    """Semantic capability promised by an adapter package."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    kind: CapabilityKind
    unit: str | None = None
    readable: bool
    writable: bool
    minimum: float | int | None = None
    maximum: float | int | None = None
    enum_values: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    optional: bool = False
    constraints: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_domain(self) -> CapabilityDeclaration:
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("minimum and maximum must be provided together")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must be less than or equal to maximum")
        if self.kind not in {CapabilityKind.INTEGER, CapabilityKind.NUMBER}:
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("numeric bounds are only valid for integer or number capabilities")
        if self.kind is CapabilityKind.ENUM and not self.enum_values:
            raise ValueError("enum capabilities require enum_values")
        if len(set(self.commands)) != len(self.commands):
            raise ValueError("commands must be unique")
        if self.writable and not self.commands:
            raise ValueError("writable capabilities require commands")
        if not self.writable and self.commands:
            raise ValueError("read-only capabilities cannot declare commands")
        return self


class AdapterManifest(StrictModel):
    """Serializable, fail-closed declaration for one adapter package."""

    schema_version: Literal["v1"] = ADAPTER_SDK_SCHEMA_VERSION
    adapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    name: str = Field(min_length=1)
    contract_version: Literal["v1"] = ADAPTER_CONTRACT_VERSION
    protocol: str = Field(min_length=1)
    package_name: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    device_types: list[DeviceType] = Field(min_length=1)
    capabilities: list[CapabilityDeclaration] = Field(min_length=1)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_declarations(self) -> AdapterManifest:
        names = [capability.name for capability in self.capabilities]
        if len(set(names)) != len(names):
            raise ValueError("capability names must be unique")
        sensitive_fragments = (
            "token",
            "password",
            "secret",
            "authorization",
            "credential",
            "api_key",
            "cookie",
        )
        for key in self.metadata:
            normalized = key.casefold().replace("-", "_")
            if any(fragment in normalized for fragment in sensitive_fragments):
                raise ValueError(f"metadata contains sensitive field {key!r}")
        return self


class CompatibilityDiagnostic(StrictModel):
    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    severity: DiagnosticSeverity
    subject: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=240)
    details: dict[str, str | int | bool] = Field(default_factory=dict)


class CapabilityCompatibility(StrictModel):
    name: str = Field(min_length=1)
    status: CapabilityCompatibilityStatus
    optional: bool
    commands: list[str] = Field(default_factory=list)


class CompatibilityReport(StrictModel):
    schema_version: Literal["v1"] = ADAPTER_SDK_SCHEMA_VERSION
    adapter_id: str = Field(min_length=1)
    contract_version: Literal["v1"] = ADAPTER_CONTRACT_VERSION
    status: CompatibilityStatus
    capabilities: list[CapabilityCompatibility] = Field(default_factory=list)
    diagnostics: list[CompatibilityDiagnostic] = Field(default_factory=list)


def sanitize_exception(error: BaseException) -> str:
    """Return a stable diagnostic without provider payloads or credentials."""

    return type(error).__name__
