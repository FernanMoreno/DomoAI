"""Transport authentication for the shared MCP gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from domoai.domain.errors import DomainError, ErrorCode


class ClientTokenRecord(BaseModel):
    """Server-owned token metadata; the raw bearer is never stored here."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: list[str] = Field(default_factory=list)
    enabled: bool = True
    expires_at: datetime | None = None


class ClientTokenDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clients: list[ClientTokenRecord]


@dataclass(frozen=True)
class StaticBearerTokenVerifier(TokenVerifier):
    """Constant-time verifier for a deployment-owned client token file."""

    _records: tuple[ClientTokenRecord, ...]

    @classmethod
    def from_file(cls, path: Path) -> StaticBearerTokenVerifier:
        try:
            document = ClientTokenDocument.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid MCP client token file: {path}") from error
        if len({record.client_id for record in document.clients}) != len(document.clients):
            raise ValueError("MCP client token client_id values must be unique")
        return cls(tuple(document.clients))

    async def verify_token(self, token: str) -> AccessToken | None:
        presented_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for record in self._records:
            if not hmac.compare_digest(presented_hash, record.token_hash):
                continue
            if not record.enabled:
                return None
            if record.expires_at is not None:
                expires_at = int(record.expires_at.timestamp())
                if expires_at <= int(datetime.now(UTC).timestamp()):
                    return None
            return AccessToken(
                token=token,
                client_id=record.client_id,
                scopes=list(record.scopes),
                expires_at=(
                    int(record.expires_at.timestamp()) if record.expires_at is not None else None
                ),
                subject=record.client_id,
                claims={"client_id": record.client_id},
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


def current_access_token() -> AccessToken | None:
    """Read the SDK-authenticated token without exposing its secret value."""

    from mcp.server.auth.middleware.auth_context import get_access_token

    return get_access_token()


def current_client_id() -> str:
    """Return only the authenticated non-secret client identifier."""

    token = current_access_token()
    return token.client_id if token is not None else "local"
