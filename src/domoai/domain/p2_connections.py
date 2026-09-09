"""Strict, secret-safe contracts for the P2 connection workflows."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from domoai.domain.models import StrictModel

_DIGEST = r"^[0-9a-f]{64}$"
_REFERENCE = r"^(?:secret:[0-9a-fA-F-]{16,128}|env:[A-Za-z_][A-Za-z0-9_]*)$"
_SENSITIVE = re.compile(
    r"(?:token|password|passwd|secret|bearer|authorization|api[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)


class EtsSourceKind(StrEnum):
    XML = "xml"
    KNXPROJ = "knxproj"


class EtsSupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


EtsCapabilityName = Literal[
    "power",
    "brightness",
    "temperature",
    "humidity",
    "occupancy",
    "battery.soc",
    "battery.power",
    "battery.capacity",
]
EtsSemanticType = Literal["light", "switch", "sensor", "energy"]


class EtsMappingCapability(StrictModel):
    name: EtsCapabilityName
    dpt: str = Field(min_length=1)
    state_group_address: str = Field(min_length=3)
    command_group_address: str | None = Field(default=None, min_length=3)


class EtsMappingEntity(StrictModel):
    entity_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    area_id: str | None = Field(default=None, max_length=128)
    semantic_type: EtsSemanticType
    manufacturer: str | None = None
    model: str | None = None
    capabilities: tuple[EtsMappingCapability, ...] = Field(min_length=1)


class EtsMappingDocument(StrictModel):
    schema_version: Literal["v1"]
    entities: tuple[EtsMappingEntity, ...] = Field(min_length=1)

    def __eq__(self, other: object) -> bool:
        other_dump = getattr(other, "model_dump", None)
        if callable(other_dump):
            return bool(self.model_dump(mode="json") == other_dump(mode="json"))
        return super().__eq__(other)


class EtsGroupAddress(StrictModel):
    address: str = Field(min_length=3, max_length=16, pattern=r"^\d+/\d+(?:/\d+)?$")
    dpt: str = Field(min_length=3, max_length=16, pattern=r"^\d{1,3}\.\d{1,3}$")
    name: str = Field(min_length=1, max_length=256)
    area: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    support: EtsSupportStatus
    capability: EtsCapabilityName | None = None

    @model_validator(mode="after")
    def validate_support(self) -> EtsGroupAddress:
        if self.support is EtsSupportStatus.SUPPORTED and self.capability is None:
            raise ValueError("supported ETS address requires a capability")
        if self.support is not EtsSupportStatus.SUPPORTED and self.capability is not None:
            raise ValueError("unsupported ETS address cannot expose a capability")
        return self


class EtsImportWarning(StrictModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=240)
    address: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def reject_secret_text(self) -> EtsImportWarning:
        if _SENSITIVE.search(self.message):
            raise ValueError("ETS warning contains sensitive material")
        return self


class EtsImportPreview(StrictModel):
    schema_version: Literal["v1"] = "v1"
    source_digest: str = Field(pattern=_DIGEST)
    source_kind: EtsSourceKind
    project_name: str = Field(min_length=1, max_length=256)
    group_addresses: tuple[EtsGroupAddress, ...] = Field(max_length=1000)
    # The proposal is validated locally; the adapter canonical model is
    # accepted by the field validator without creating a domain dependency.
    mapping: EtsMappingDocument | None = None
    warnings: tuple[EtsImportWarning, ...] = Field(max_length=256)
    review_required: Literal[True] = True

    @field_validator("mapping", mode="before")
    @classmethod
    def coerce_mapping_model(cls, value: object) -> object:
        if isinstance(value, dict):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        return value

    @model_validator(mode="after")
    def validate_preview(self) -> EtsImportPreview:
        addresses = [item.address for item in self.group_addresses]
        if len(addresses) != len(set(addresses)):
            raise ValueError("ETS group addresses must be unique")
        codes = [item.code for item in self.warnings]
        if len(codes) != len(set(codes)):
            raise ValueError("ETS warning codes must be unique")
        return self


class KnxSecureMode(StrEnum):
    IP_SECURE = "ip_secure"
    DATA_SECURE = "data_secure"
    IP_AND_DATA_SECURE = "ip_and_data_secure"


class KnxSecureOptions(StrictModel):
    mode: KnxSecureMode
    keyring_path: str | None = Field(default=None, min_length=1, max_length=512)
    interface: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: int | None = Field(default=None, ge=0, le=65535)
    credential_ref: str = Field(pattern=_REFERENCE)

    @model_validator(mode="after")
    def validate_options(self) -> KnxSecureOptions:
        if self.mode in {
            KnxSecureMode.DATA_SECURE,
            KnxSecureMode.IP_AND_DATA_SECURE,
        } and self.keyring_path is None:
            raise ValueError("Data Secure requires a keyring path")
        if (
            self.mode is KnxSecureMode.IP_SECURE
            and self.user_id is None
            and self.keyring_path is None
        ):
            raise ValueError("IP Secure requires a user id or keyring path")
        return self


class OAuthAuthorizationRequest(StrictModel):
    schema_version: Literal["v1"] = "v1"
    state: str = Field(min_length=16, max_length=256)
    authorization_url: str = Field(min_length=1, max_length=2048)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry(self) -> OAuthAuthorizationRequest:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("OAuth expiry must be timezone-aware")
        return self


class OAuthCredentialRecord(StrictModel):
    schema_version: Literal["v1"] = "v1"
    provider: str = Field(min_length=1, max_length=128)
    credential_ref: str = Field(pattern=_REFERENCE)
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = Field(default=(), max_length=64)
    refreshable: bool = False

    @model_validator(mode="after")
    def validate_record(self) -> OAuthCredentialRecord:
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("OAuth credential expiry must be timezone-aware")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("OAuth scopes must be unique")
        return self


class OAuthMetadataSource(StrEnum):
    OAUTH_AUTHORIZATION_SERVER = "oauth_authorization_server"
    OPENID_CONFIGURATION = "openid_configuration"
    MANUAL = "manual"


def _validate_public_oauth_url(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain credentials")
    return value


class OAuthProviderMetadata(StrictModel):
    """Discovered OAuth/OIDC endpoints, deliberately excluding credentials."""

    schema_version: Literal["v1"] = "v1"
    issuer: str | None = Field(default=None, max_length=2048)
    authorization_url: str = Field(min_length=1, max_length=2048)
    token_url: str = Field(min_length=1, max_length=2048)
    revoke_url: str | None = Field(default=None, max_length=2048)
    scopes: tuple[str, ...] = Field(default=(), max_length=64)
    source: OAuthMetadataSource

    @model_validator(mode="after")
    def validate_metadata(self) -> OAuthProviderMetadata:
        for field_name in ("issuer", "authorization_url", "token_url", "revoke_url"):
            _validate_public_oauth_url(getattr(self, field_name), field_name)
        if len(set(self.scopes)) != len(self.scopes) or any(
            not scope.strip() for scope in self.scopes
        ):
            raise ValueError("OAuth scopes must be unique and non-empty")
        return self


class OAuthMetadataDiscoveryResult(StrictModel):
    """Result of bounded metadata discovery with an explicit manual fallback."""

    schema_version: Literal["v1"] = "v1"
    metadata: OAuthProviderMetadata | None = None
    manual_required: bool = False
    diagnostic_code: str | None = Field(default=None, max_length=64)
    diagnostic_message: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def validate_result(self) -> OAuthMetadataDiscoveryResult:
        if self.manual_required and self.metadata is not None:
            raise ValueError("manual fallback cannot include metadata")
        if self.manual_required and (not self.diagnostic_code or not self.diagnostic_message):
            raise ValueError("manual fallback requires a diagnostic")
        if not self.manual_required and self.metadata is None:
            raise ValueError("successful OAuth metadata discovery requires metadata")
        if _SENSITIVE.search(self.diagnostic_message or ""):
            raise ValueError("OAuth diagnostic contains sensitive material")
        return self


__all__ = [
    "EtsGroupAddress",
    "EtsImportPreview",
    "EtsImportWarning",
    "EtsMappingCapability",
    "EtsMappingDocument",
    "EtsMappingEntity",
    "EtsCapabilityName",
    "EtsSourceKind",
    "EtsSupportStatus",
    "KnxSecureMode",
    "KnxSecureOptions",
    "OAuthAuthorizationRequest",
    "OAuthCredentialRecord",
    "OAuthMetadataDiscoveryResult",
    "OAuthMetadataSource",
    "OAuthProviderMetadata",
]
