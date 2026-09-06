"""Transport authentication for the shared MCP gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import AuthorityContext, PrincipalRole


class ClientTokenRecord(BaseModel):
    """Server-owned token metadata; the raw bearer is never stored here."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    tenant_id: str = Field(default="default", min_length=1)
    household_ids: list[str] = Field(default_factory=lambda: ["default"], min_length=1)
    roles: list[PrincipalRole] = Field(default_factory=list)
    area_ids: list[str] = Field(default_factory=list)
    device_ids: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_expiry(self) -> ClientTokenRecord:
        for field_name, value in (
            ("created_at", self.created_at),
            ("expires_at", self.expires_at),
            ("revoked_at", self.revoked_at),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"client token {field_name} must be timezone-aware")
        if (
            self.created_at is not None
            and self.expires_at is not None
            and self.expires_at <= self.created_at
        ):
            raise ValueError("client token expiry must follow creation")
        if (
            self.created_at is not None
            and self.revoked_at is not None
            and self.revoked_at < self.created_at
        ):
            raise ValueError("client token revocation cannot precede creation")
        if len(set(self.household_ids)) != len(self.household_ids):
            raise ValueError("client token household IDs must be unique")
        return self


class ClientTokenDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clients: list[ClientTokenRecord]

    @model_validator(mode="after")
    def validate_unique_clients(self) -> ClientTokenDocument:
        client_ids = [record.client_id for record in self.clients]
        token_hashes = [record.token_hash for record in self.clients]
        if len(set(client_ids)) != len(client_ids):
            raise ValueError("MCP client token client_id values must be unique")
        if len(set(token_hashes)) != len(token_hashes):
            raise ValueError("MCP client token token_hash values must be unique")
        return self


@dataclass
class StaticBearerTokenVerifier(TokenVerifier):
    """Constant-time verifier for a deployment-owned client token file."""

    _records: tuple[ClientTokenRecord, ...]
    _path: Path | None = None

    @staticmethod
    def _load_records(path: Path) -> tuple[ClientTokenRecord, ...]:
        document = ClientTokenDocument.model_validate_json(path.read_text(encoding="utf-8"))
        return tuple(document.clients)

    @classmethod
    def from_file(cls, path: Path) -> StaticBearerTokenVerifier:
        try:
            records = cls._load_records(path)
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid MCP client token file: {path}") from error
        return cls(records, path)

    def reload(self) -> None:
        """Atomically replace records; retain the previous set on parse failure."""

        if self._path is None:
            raise ValueError("token verifier was not loaded from a file")
        try:
            records = self._load_records(self._path)
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid MCP client token file: {self._path}") from error
        self._records = records

    async def verify_token(self, token: str) -> AccessToken | None:
        presented_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for record in self._records:
            if not hmac.compare_digest(presented_hash, record.token_hash):
                continue
            if not record.enabled:
                return None
            if record.revoked_at is not None:
                return None
            if record.expires_at is not None:
                expires_at = int(record.expires_at.astimezone(UTC).timestamp())
                if expires_at <= int(datetime.now(UTC).timestamp()):
                    return None
            roles = record.roles or _roles_from_scopes(record.scopes)
            authority = AuthorityContext(
                tenant_id=record.tenant_id,
                household_id=record.household_ids[0],
                household_ids=list(record.household_ids),
                principal_id=record.client_id,
                roles=roles,
                area_ids=list(record.area_ids),
                device_ids=list(record.device_ids),
                capabilities=list(record.capabilities),
                operations=list(record.operations),
            )
            return AccessToken(
                token=token,
                client_id=record.client_id,
                scopes=list(record.scopes),
                expires_at=(
                    int(record.expires_at.astimezone(UTC).timestamp())
                    if record.expires_at is not None
                    else None
                ),
                subject=record.client_id,
                claims={
                    "client_id": record.client_id,
                    "authority": authority.model_dump(mode="json"),
                },
            )
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(clients={len(self._records)})"


def require_client_scope(token: AccessToken | None, scope: str) -> None:
    """Enforce a server-owned client scope when a network token is present."""

    if token is None or scope in token.scopes or "*" in token.scopes:
        return
    raise DomainError(
        ErrorCode.INSUFFICIENT_SCOPE,
        "The authenticated MCP client lacks the required scope",
        details={"required_scope": scope},
    )


def _roles_from_scopes(scopes: list[str]) -> list[PrincipalRole]:
    """Keep legacy token files usable while assigning the least role needed."""

    if "*" in scopes:
        return [PrincipalRole.OWNER]
    if "mutate" in scopes or "home:write" in scopes:
        return [PrincipalRole.OPERATOR]
    if "plan" in scopes or "home:plan" in scopes:
        return [PrincipalRole.PLANNER]
    return [PrincipalRole.VIEWER]


def current_access_token() -> AccessToken | None:
    """Read the SDK-authenticated token without exposing its secret value."""

    from mcp.server.auth.middleware.auth_context import get_access_token

    return get_access_token()


def current_client_id() -> str:
    """Return only the authenticated non-secret client identifier."""

    token = current_access_token()
    return token.client_id if token is not None else "local"


def current_authority() -> AuthorityContext | None:
    """Return verified non-secret identity claims for the current request."""

    token = current_access_token()
    if token is None:
        return None
    claims = token.claims or {}
    raw_authority = claims.get("authority")
    if not isinstance(raw_authority, dict):
        return AuthorityContext(
            principal_id=token.client_id,
            roles=_roles_from_scopes(token.scopes),
        )
    try:
        return AuthorityContext.model_validate(raw_authority)
    except (TypeError, ValueError):
        return None
