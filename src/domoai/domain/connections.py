"""Secret-safe domain contracts for the connection control plane."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .connector_recipes import ConnectorRecipe
from .models import ScalarValue, StrictModel

_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "password",
    "passwd",
    "secret",
    "bearer",
    "credential",
    "authorization",
    "api_key",
    "private_key",
    "passphrase",
)
_CREDENTIAL_REF_PATTERN = re.compile(
    r"^(?:secret:[0-9a-fA-F-]{16,128}|env:[A-Za-z_][A-Za-z0-9_]*)$"
)
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?:token|password|passwd|secret|bearer|authorization|api[_-]?key|private[_-]?key|passphrase)"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@", re.IGNORECASE
)

type ScalarMap = dict[str, ScalarValue]


def _contains_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _validate_secret_safe_map(field_name: str, value: Any) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field_name} keys must be strings")
        if _contains_sensitive_key(key):
            raise ValueError(f"{field_name} contains sensitive fields")
        if not isinstance(item, (str, int, float, bool)):
            raise ValueError(f"{field_name} values must be scalar and secret-safe")
        if isinstance(item, str) and (
            _SENSITIVE_TEXT_PATTERN.search(item) is not None
            or _CREDENTIAL_URL_PATTERN.search(item) is not None
        ):
            raise ValueError(f"{field_name} contains sensitive material")


def _validate_secret_safe_text(field_name: str, value: Any) -> None:
    if isinstance(value, str) and _SENSITIVE_TEXT_PATTERN.search(value):
        raise ValueError(f"{field_name} contains sensitive material")


def _validate_unique_diagnostics(values: tuple[ConnectionDiagnostic, ...]) -> None:
    codes = [diagnostic.code for diagnostic in values]
    if len(codes) != len(set(codes)):
        raise ValueError("diagnostic codes must be unique")


class ConnectionProfile(StrictModel):
    """Secret-safe description of one concrete connection instance."""

    id: UUID
    connector_id: str = Field(min_length=1, max_length=64)
    unique_id: str = Field(min_length=1, max_length=256)
    endpoint: ScalarMap
    options: ScalarMap
    credential_ref: str | None = None
    mapping_ref: str | None = Field(default=None, min_length=1, max_length=512)
    enabled: bool = True
    schema_version: Literal["v1"] = "v1"
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_metadata(cls, value: Any) -> Any:
        if isinstance(value, dict):
            _validate_secret_safe_map("endpoint", value.get("endpoint"))
            _validate_secret_safe_map("options", value.get("options"))
        return value

    @model_validator(mode="after")
    def validate_profile(self) -> ConnectionProfile:
        if self.credential_ref is not None and not _CREDENTIAL_REF_PATTERN.fullmatch(
            self.credential_ref
        ):
            raise ValueError("credential_ref must be an opaque secret: or env: reference")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        return self


class TransportStatus(StrEnum):
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    CONNECTED = "connected"
    AUTH_REQUIRED = "auth_required"
    AUTH_FAILED = "auth_failed"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    INCOMPATIBLE = "incompatible"
    DISABLED = "disabled"


class InventoryStatus(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    MAPPING_REQUIRED = "mapping_required"
    PAIRING_REQUIRED = "pairing_required"
    EMPTY = "empty"
    FAILED = "failed"


class ProductStatus(StrEnum):
    CONNECTED = "connected"
    NEEDS_SETUP = "needs_setup"
    REAUTH_REQUIRED = "reauth_required"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ConnectionDraft(StrictModel):
    """Not-yet-validated connection data without raw credentials."""

    connector_id: str = Field(min_length=1, max_length=64)
    unique_id: str = Field(min_length=1, max_length=256)
    endpoint: ScalarMap
    options: ScalarMap
    credential_ref: str | None = None
    mapping_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_metadata(cls, value: Any) -> Any:
        if isinstance(value, dict):
            _validate_secret_safe_map("endpoint", value.get("endpoint"))
            _validate_secret_safe_map("options", value.get("options"))
        return value

    @model_validator(mode="after")
    def validate_draft(self) -> ConnectionDraft:
        if self.credential_ref is not None and not _CREDENTIAL_REF_PATTERN.fullmatch(
            self.credential_ref
        ):
            raise ValueError("credential_ref must be an opaque secret: or env: reference")
        return self


class ConnectionDiagnostic(StrictModel):
    """Actionable connection information that cannot contain secret material."""

    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    severity: DiagnosticSeverity
    retryable: bool
    user_action: str | None = Field(default=None, min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=240)
    technical_details: ScalarMap = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_content(cls, value: Any) -> Any:
        if isinstance(value, dict):
            _validate_secret_safe_map("technical_details", value.get("technical_details"))
            _validate_secret_safe_text("message", value.get("message"))
            _validate_secret_safe_text("user_action", value.get("user_action"))
        return value


class ProbeResult(StrictModel):
    """Independent transport, inventory, and product health projection."""

    transport_status: TransportStatus
    inventory_status: InventoryStatus
    product_status: ProductStatus
    diagnostics: tuple[ConnectionDiagnostic, ...] = ()

    @model_validator(mode="after")
    def validate_diagnostics(self) -> ProbeResult:
        _validate_unique_diagnostics(self.diagnostics)
        return self


class ValidationResult(StrictModel):
    """Secret-safe outcome of validating a connection draft."""

    valid: bool
    diagnostics: tuple[ConnectionDiagnostic, ...] = ()
    normalized_draft: ConnectionDraft | None = None

    @model_validator(mode="after")
    def validate_result(self) -> ValidationResult:
        _validate_unique_diagnostics(self.diagnostics)
        if self.valid and any(
            diagnostic.severity is DiagnosticSeverity.ERROR for diagnostic in self.diagnostics
        ):
            raise ValueError("valid results cannot contain error diagnostics")
        return self


class OnboardingSessionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class SetupTransitionStatus(StrEnum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    FAILED = "failed"


class ConnectionCandidate(StrictModel):
    """Secret-safe discovered or manually supplied connection candidate."""

    connector_id: str = Field(min_length=1, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    endpoint: ScalarMap
    metadata: ScalarMap = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_metadata(cls, value: Any) -> Any:
        if isinstance(value, dict):
            _validate_secret_safe_map("endpoint", value.get("endpoint"))
            _validate_secret_safe_map("metadata", value.get("metadata"))
        return value


class SetupFieldType(StrEnum):
    """Small, renderer-independent vocabulary for provider setup inputs."""

    TEXT = "text"
    URL = "url"
    PASSWORD = "password"
    NUMBER = "number"
    SELECT = "select"


class SetupFieldDescriptor(StrictModel):
    """Secret-free metadata used to render one setup input."""

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=128)
    input_type: SetupFieldType
    required: bool = True
    secret: bool = False
    help_text: str = Field(default="", max_length=256)
    options: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_options(self) -> SetupFieldDescriptor:
        if len(set(self.options)) != len(self.options):
            raise ValueError("field options must be unique")
        if self.input_type is SetupFieldType.SELECT and not self.options:
            raise ValueError("select fields require options")
        if self.input_type is not SetupFieldType.SELECT and self.options:
            raise ValueError("only select fields may declare options")
        return self


class SetupStep(StrictModel):
    """One onboarding step described by input names, never input values."""

    step_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=256)
    input_keys: tuple[str, ...] = ()
    secret_input_keys: tuple[str, ...] = ()
    fields: tuple[SetupFieldDescriptor, ...] = ()

    @model_validator(mode="after")
    def validate_input_keys(self) -> SetupStep:
        if len(set(self.input_keys)) != len(self.input_keys):
            raise ValueError("input_keys must be unique")
        if len(set(self.secret_input_keys)) != len(self.secret_input_keys):
            raise ValueError("secret_input_keys must be unique")
        if not set(self.secret_input_keys).issubset(self.input_keys):
            raise ValueError("secret_input_keys must be a subset of input_keys")
        if self.fields:
            field_keys = [field.key for field in self.fields]
            if len(field_keys) != len(set(field_keys)) or set(field_keys) != set(self.input_keys):
                raise ValueError("fields must match input_keys exactly")
            secret_keys = {field.key for field in self.fields if field.secret}
            if secret_keys != set(self.secret_input_keys):
                raise ValueError("fields must match secret_input_keys exactly")
        return self


class ProviderCatalogEntry(StrictModel):
    """Secret-free provider metadata exposed to operator onboarding clients."""

    connector_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    protocol: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    supports_discovery: bool = True
    supports_manual_setup: bool = True
    steps: tuple[SetupStep, ...] = ()
    recipe: ConnectorRecipe | None = None

    @classmethod
    def from_manifest(cls, manifest: Any) -> ProviderCatalogEntry:
        return cls(
            connector_id=manifest.connector_id,
            display_name=manifest.display_name,
            protocol=manifest.protocol,
            version=manifest.version,
            supports_discovery=manifest.supports_discovery,
            supports_manual_setup=manifest.supports_manual_setup,
            steps=manifest.steps,
            recipe=getattr(manifest, "recipe", None),
        )


class OnboardingSession(StrictModel):
    """Explicit lifecycle state for a multi-step provider setup flow."""

    id: UUID
    connector_id: str = Field(min_length=1, max_length=64)
    candidate_id: str | None = Field(default=None, min_length=1, max_length=256)
    status: OnboardingSessionStatus
    current_step_id: str | None = Field(default=None, min_length=1, max_length=64)
    expires_at: datetime
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_timestamps(self) -> OnboardingSession:
        timestamps = (self.expires_at, self.created_at, self.updated_at)
        if any(
            timestamp.tzinfo is None or timestamp.utcoffset() is None
            for timestamp in timestamps
        ):
            raise ValueError("onboarding session timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be greater than created_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        return self


class SetupTransition(StrictModel):
    """Explicit result of advancing an onboarding session."""

    session_id: UUID
    status: SetupTransitionStatus
    next_step: SetupStep | None = None
    draft: ConnectionDraft | None = None
    diagnostics: tuple[ConnectionDiagnostic, ...] = ()

    @model_validator(mode="after")
    def validate_transition(self) -> SetupTransition:
        _validate_unique_diagnostics(self.diagnostics)
        if self.status is SetupTransitionStatus.CONTINUE and self.next_step is None:
            raise ValueError("continue transitions require next_step")
        if self.status is SetupTransitionStatus.COMPLETED and self.next_step is not None:
            raise ValueError("completed transitions cannot contain next_step")
        return self


__all__ = [
    "ConnectionDiagnostic",
    "ConnectionCandidate",
    "ConnectionDraft",
    "ConnectionProfile",
    "DiagnosticSeverity",
    "InventoryStatus",
    "OnboardingSession",
    "OnboardingSessionStatus",
    "ProbeResult",
    "ProductStatus",
    "ProviderCatalogEntry",
    "ScalarMap",
    "SetupFieldDescriptor",
    "SetupFieldType",
    "SetupStep",
    "SetupTransition",
    "SetupTransitionStatus",
    "TransportStatus",
    "ValidationResult",
]
