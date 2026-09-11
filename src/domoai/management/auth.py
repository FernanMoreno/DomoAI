"""Management bind and authentication policy."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass

from mcp.server.auth.provider import AccessToken

from domoai.application.authority import authority_from_token_claims
from domoai.domain.models import AuthorityContext, PrincipalRole

_READ_SCOPE_ALIASES = frozenset(
    {"read", "home:read", "plan", "home:plan", "mutate", "home:write", "manage", "*"}
)
_MANAGE_SCOPES = frozenset({"manage", "*"})
_EXPLICIT_AUTHORITY_FIELDS = frozenset(
    {"tenant_id", "household_id", "household_ids", "principal_id", "roles"}
)
_READ_ONLY_POST_PATHS = frozenset(
    {
        "/api/v1/connections/discovery",
        "/api/v1/mqtt/discovery/preview",
        "/api/v1/knx/ets/preview",
        "/api/v1/home-assistant/oauth/discover",
    }
)


def is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ManagementAuth:
    def __init__(
        self,
        host: str,
        *,
        token: str | None,
        tls_enabled: bool,
        external_auth: bool = False,
    ) -> None:
        if token is not None and not token:
            raise ValueError("management authentication token must be non-empty")
        if not is_loopback_host(host) and token is None and not external_auth:
            raise ValueError("non-loopback management requires authentication")
        if not is_loopback_host(host) and not tls_enabled:
            raise ValueError("non-loopback management requires HTTPS")
        self.host = host
        self._token_digest = (
            hashlib.sha256(token.encode("utf-8")).digest() if token is not None else None
        )

    def authorized(self, header: str | None) -> bool:
        if self._token_digest is None:
            return True
        if not header or not header.casefold().startswith("bearer "):
            return False
        token = header[7:].strip()
        if not token:
            return False
        return hmac.compare_digest(
            hashlib.sha256(token.encode("utf-8")).digest(), self._token_digest
        )


@dataclass(frozen=True)
class ManagementPrincipal:
    """Non-secret request identity used by the Management authorization layer."""

    client_id: str
    scopes: frozenset[str]
    authority: AuthorityContext
    explicit_authority: bool = False

    @classmethod
    def from_access_token(cls, token: AccessToken) -> ManagementPrincipal:
        claims = token.claims or {}
        raw = claims.get("authority")
        explicit = isinstance(raw, dict) and _EXPLICIT_AUTHORITY_FIELDS.issubset(raw)
        if "authority" in claims:
            if not explicit:
                raise ValueError("management authority claims are incomplete")
            AuthorityContext.model_validate(raw)
        return cls(
            client_id=token.client_id,
            scopes=frozenset(token.scopes),
            authority=authority_from_token_claims(
                client_id=token.client_id,
                scopes=token.scopes,
                claims=claims,
            ),
            explicit_authority=explicit
            and claims.get("management_authority_explicit", True) is True,
        )

    @classmethod
    def static(cls, authority: AuthorityContext | None = None) -> ManagementPrincipal:
        return cls(
            client_id="management-static",
            scopes=frozenset({"*"}),
            authority=authority
            or AuthorityContext(
                principal_id="management-static",
                roles=[PrincipalRole.SERVICE],
            ),
            explicit_authority=True,
        )

    def allows(self, required_scope: str) -> bool:
        if required_scope == "read":
            return bool(self.scopes & _READ_SCOPE_ALIASES)
        return (
            required_scope == "manage"
            and self.explicit_authority
            and bool(self.scopes & _MANAGE_SCOPES)
        )


def required_management_scope(method: str, path: str) -> str:
    """Classify an HTTP request before its Starlette handler runs."""

    normalized_method = method.upper()
    if normalized_method in {"GET", "HEAD", "OPTIONS"}:
        return "read"
    if path in _READ_ONLY_POST_PATHS:
        return "read"
    if normalized_method == "POST" and path.startswith("/api/v1/connections/"):
        if path.endswith("/validate"):
            return "read"
    return "manage"


def authority_operation_for_scope(required_scope: str) -> str:
    return "list" if required_scope == "read" else "manage_connections"


def is_public_management_shell(method: str, path: str) -> bool:
    """Only the exact static landing page bypasses authentication."""

    return method in {"GET", "HEAD"} and path == "/"


__all__ = [
    "ManagementAuth",
    "ManagementPrincipal",
    "authority_operation_for_scope",
    "is_loopback_host",
    "is_public_management_shell",
    "required_management_scope",
]
