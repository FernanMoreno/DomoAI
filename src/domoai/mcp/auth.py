"""Transport authentication for the shared MCP gateway."""

from __future__ import annotations

from mcp.server.auth.provider import AccessToken

from domoai.application.authority import authority_from_token_claims, roles_from_scopes
from domoai.application.client_tokens import (
    ClientTokenDocument as ClientTokenDocument,
)
from domoai.application.client_tokens import (
    ClientTokenRecord as ClientTokenRecord,
)
from domoai.application.client_tokens import (
    CredentialRecordSnapshot as CredentialRecordSnapshot,
)
from domoai.application.client_tokens import (
    CredentialSnapshot as CredentialSnapshot,
)
from domoai.application.client_tokens import (
    CredentialSourceSnapshot as CredentialSourceSnapshot,
)
from domoai.application.client_tokens import (
    StaticBearerTokenVerifier as StaticBearerTokenVerifier,
)
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import AuthorityContext, PrincipalRole


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
