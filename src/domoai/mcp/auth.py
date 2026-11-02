"""Transport authentication for the shared MCP gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal

from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from domoai.application.authority import authority_from_token_claims, roles_from_scopes
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
    # Preserve original provisioning intent when token tools serialize defaults.
    management_authority_explicit: bool | None = None
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
        declared = bool(self.roles) and {"roles", "tenant_id", "household_ids"}.issubset(
            self.model_fields_set
        )
        self.management_authority_explicit = (
            declared and self.management_authority_explicit is not False
        )
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


@dataclass(frozen=True, slots=True, repr=False)
class CredentialSourceSnapshot:
    canonical_path: str
    device_id: int | None
    inode: int | None


@dataclass(frozen=True, slots=True, repr=False)
class CredentialRecordSnapshot:
    client_id: str
    token_hash: str
    scopes: tuple[str, ...]
    enabled: bool
    created_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    tenant_id: str
    household_ids: tuple[str, ...]
    roles: tuple[PrincipalRole, ...]
    management_authority_explicit: bool | None
    provided_fields: frozenset[str]
    area_ids: tuple[str, ...]
    device_ids: tuple[str, ...]
    capabilities: tuple[str, ...]
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class CredentialSnapshot:
    schema_version: Literal["domoai.credential-snapshot.v1"]
    source: CredentialSourceSnapshot | None
    records: tuple[CredentialRecordSnapshot, ...]
    revision_digest: str


def _snapshot_record(record: ClientTokenRecord) -> CredentialRecordSnapshot:
    """Validate active shapes without re-inferring defaults or provisioning intent."""
    message = "invalid active credential snapshot"
    if not isinstance(record, ClientTokenRecord):
        raise ValueError(message)
    for value in (record.client_id, record.tenant_id, record.token_hash):
        if type(value) is not str or not value:
            raise ValueError(message)
    if len(record.token_hash) != 64 or any(
        char not in "0123456789abcdef" for char in record.token_hash
    ):
        raise ValueError(message)
    if type(record.enabled) is not bool or (
        record.management_authority_explicit is not None
        and type(record.management_authority_explicit) is not bool
    ):
        raise ValueError(message)

    def strings(values: list[str]) -> tuple[str, ...]:
        if type(values) is not list or any(type(value) is not str for value in values):
            raise ValueError(message)
        return tuple(values)

    def instant(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(message)
        normalized = value.astimezone(UTC)
        # astimezone can retain a mutable subclass (or return the original object).
        return datetime(
            normalized.year,
            normalized.month,
            normalized.day,
            normalized.hour,
            normalized.minute,
            normalized.second,
            normalized.microsecond,
            tzinfo=UTC,
            fold=normalized.fold,
        )

    households = strings(record.household_ids)
    if not households or len(set(households)) != len(households):
        raise ValueError(message)
    if type(record.roles) is not list or any(
        not isinstance(role, PrincipalRole) for role in record.roles
    ):
        raise ValueError(message)
    provided = record.model_fields_set
    if type(provided) is not set or any(type(name) is not str for name in provided):
        raise ValueError(message)
    if not provided.issubset(ClientTokenRecord.model_fields):
        raise ValueError(message)
    created, expires, revoked = map(
        instant, (record.created_at, record.expires_at, record.revoked_at)
    )
    if created is not None and (
        (expires is not None and expires <= created) or (revoked is not None and revoked < created)
    ):
        raise ValueError(message)
    return CredentialRecordSnapshot(
        client_id=record.client_id,
        token_hash=record.token_hash,
        scopes=strings(record.scopes),
        enabled=record.enabled,
        created_at=created,
        expires_at=expires,
        revoked_at=revoked,
        tenant_id=record.tenant_id,
        household_ids=households,
        roles=tuple(record.roles),
        management_authority_explicit=record.management_authority_explicit,
        provided_fields=frozenset(provided),
        area_ids=strings(record.area_ids),
        device_ids=strings(record.device_ids),
        capabilities=strings(record.capabilities),
        operations=strings(record.operations),
    )


def _snapshot_json_value(value: object) -> str | list[str]:
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, frozenset):
        return sorted(value)
    raise TypeError("invalid active credential snapshot")


@dataclass
class StaticBearerTokenVerifier(TokenVerifier):
    """Constant-time verifier for a deployment-owned client token file."""

    _records: tuple[ClientTokenRecord, ...]
    _path: Path | None = None
    _source: CredentialSourceSnapshot | None = field(default=None, init=False, repr=False)
    _snapshot_lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    @staticmethod
    def _load_state(
        path: Path,
    ) -> tuple[tuple[ClientTokenRecord, ...], CredentialSourceSnapshot | None]:
        canonical = None
        expected = None
        try:
            canonical = path.resolve(strict=True)
            expected = canonical.stat()
        except (OSError, RuntimeError):
            pass  # Legacy loading may succeed even without provable path metadata.
        source = None
        with path.open(encoding="utf-8") as handle:
            try:
                actual = os.fstat(handle.fileno())
            except OSError:
                actual = None
            content = handle.read()
        if canonical is not None and expected is not None and actual is not None:
            if (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino):
                physical = actual.st_ino > 0 and actual.st_dev >= 0
                source = CredentialSourceSnapshot(
                    str(canonical),
                    actual.st_dev if physical else None,
                    actual.st_ino if physical else None,
                )
        document = ClientTokenDocument.model_validate_json(content)
        return tuple(document.clients), source

    @staticmethod
    def _load_records(path: Path) -> tuple[ClientTokenRecord, ...]:
        return StaticBearerTokenVerifier._load_state(path)[0]

    @classmethod
    def from_file(cls, path: Path) -> StaticBearerTokenVerifier:
        try:
            records, source = cls._load_state(path)
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid MCP client token file: {path}") from error
        verifier = cls(records, path)
        verifier._source = source
        return verifier

    def reload(self) -> None:
        """Atomically replace records; retain the previous set on parse failure."""

        if self._path is None:
            raise ValueError("token verifier was not loaded from a file")
        try:
            records, source = self._load_state(self._path)
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid MCP client token file: {self._path}") from error
        with self._snapshot_lock:
            self._records = records
            self._source = source

    def credential_snapshot(self) -> CredentialSnapshot:
        """Describe one coherent active load, without file IO or bearer material."""
        with self._snapshot_lock:
            if type(self._records) is not tuple:
                raise ValueError("invalid active credential snapshot")
            try:
                records = tuple(_snapshot_record(record) for record in self._records)
            except (AttributeError, TypeError, ValueError, OverflowError) as error:
                raise ValueError("invalid active credential snapshot") from error
            source = self._source
        payload = {
            "schema_version": "domoai.credential-snapshot.v1",
            "source": asdict(source) if source is not None else None,
            "records": [asdict(record) for record in records],
        }
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
            default=_snapshot_json_value,
        ).encode("utf-8")
        return CredentialSnapshot(
            "domoai.credential-snapshot.v1", source, records, hashlib.sha256(serialized).hexdigest()
        )

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
                    "management_authority_explicit": record.management_authority_explicit,
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


def authority_from_access_token(token: AccessToken) -> AuthorityContext:
    """Decode verified, non-secret authority claims from one access token."""

    return authority_from_token_claims(
        client_id=token.client_id,
        scopes=token.scopes,
        claims=token.claims or {},
    )


def _roles_from_scopes(scopes: list[str]) -> list[PrincipalRole]:
    """Keep legacy token files usable while assigning the least role needed."""

    return roles_from_scopes(scopes)


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
    return authority_from_access_token(token)
