"""Home Assistant OAuth session and encrypted credential orchestration."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from domoai.adapters.home_assistant.oauth import (
    HomeAssistantOAuthClient,
    HomeAssistantOAuthConfig,
    HomeAssistantOAuthError,
    OAuthToken,
)
from domoai.domain.p2_connections import OAuthAuthorizationRequest, OAuthCredentialRecord
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.ports import AuditSinkPort
from domoai.security.secrets import SecretStorePort

_TOKEN_SCHEMA_VERSION = "v1"


class HomeAssistantOAuthServiceError(HomeAssistantOAuthError):
    """Sanitized OAuth workflow error."""


@dataclass(frozen=True)
class _AuthorizationSession:
    code_verifier: str
    redirect_uri: str
    config: HomeAssistantOAuthConfig
    expires_at: datetime


class HomeAssistantOAuthService:
    """Own one-time state and keep OAuth token values out of application models."""

    def __init__(
        self,
        secret_store: SecretStorePort,
        client: HomeAssistantOAuthClient | Any,
        *,
        clock: Clock | None = None,
        state_ttl_seconds: int = 600,
        audit: AuditSinkPort | None = None,
    ) -> None:
        if state_ttl_seconds <= 0:
            raise ValueError("OAuth state TTL must be positive")
        self.secret_store = secret_store
        self.client = client
        self.clock = clock or SystemClock()
        self.audit = audit
        self.state_ttl = timedelta(seconds=state_ttl_seconds)
        self._sessions: dict[str, _AuthorizationSession] = {}

    def begin(self, config: HomeAssistantOAuthConfig) -> OAuthAuthorizationRequest:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        expires_at = self.clock.now().astimezone(UTC) + self.state_ttl
        self._sessions[state] = _AuthorizationSession(
            verifier, config.redirect_uri, config, expires_at
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": config.client_id,
                "redirect_uri": config.redirect_uri,
                "scope": " ".join(config.scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in config.authorization_url else "?"
        return OAuthAuthorizationRequest(
            state=state,
            authorization_url=f"{config.authorization_url}{separator}{query}",
            expires_at=expires_at,
        )

    async def complete(
        self,
        config: HomeAssistantOAuthConfig | None,
        *,
        state: str,
        code: str,
    ) -> OAuthCredentialRecord:
        session = self._sessions.pop(state, None)
        now = self.clock.now().astimezone(UTC)
        if session is None:
            raise HomeAssistantOAuthServiceError("OAuth state is invalid or already used")
        if now >= session.expires_at:
            raise HomeAssistantOAuthServiceError("OAuth state is expired")
        resolved_config = config or session.config
        if session.redirect_uri != resolved_config.redirect_uri:
            raise HomeAssistantOAuthServiceError("OAuth redirect URI is invalid")
        try:
            token = await self.client.exchange_code(
                resolved_config, code=code, code_verifier=session.code_verifier
            )
            return self._store_token(resolved_config, token, operation="oauth_completed")
        except HomeAssistantOAuthServiceError:
            raise
        except HomeAssistantOAuthError as error:
            raise HomeAssistantOAuthServiceError("OAuth callback failed") from error

    async def refresh(
        self, config: HomeAssistantOAuthConfig, record: OAuthCredentialRecord
    ) -> OAuthCredentialRecord:
        token = self._resolve_token(record)
        if token.refresh_token is None:
            raise HomeAssistantOAuthServiceError("OAuth credential is not refreshable")
        try:
            rotated = await self.client.refresh(config, refresh_token=token.refresh_token)
            if rotated.refresh_token is None:
                rotated = replace(rotated, refresh_token=token.refresh_token)
            replacement = self._store_token(config, rotated, operation="oauth_refreshed")
            self._delete(record.credential_ref)
            return replacement
        except HomeAssistantOAuthServiceError:
            raise
        except HomeAssistantOAuthError as error:
            raise HomeAssistantOAuthServiceError("OAuth token refresh failed") from error

    async def revoke(
        self, config: HomeAssistantOAuthConfig, record: OAuthCredentialRecord
    ) -> None:
        token = self._resolve_token(record)
        try:
            await self.client.revoke(config, token=token.access_token)
            self._delete(record.credential_ref)
            self._audit("oauth_revoked", record)
        except HomeAssistantOAuthServiceError:
            raise
        except HomeAssistantOAuthError as error:
            raise HomeAssistantOAuthServiceError("OAuth token revocation failed") from error

    def migrate(self, config: HomeAssistantOAuthConfig, token: str) -> OAuthCredentialRecord:
        if not isinstance(token, str) or not token.strip():
            raise HomeAssistantOAuthServiceError("legacy token is invalid")
        return self._store_token(config, OAuthToken(access_token=token), operation="oauth_migrated")

    def validate_record(self, record: OAuthCredentialRecord) -> OAuthCredentialRecord:
        """Verify that an opaque record points to a valid stored OAuth bundle."""

        if record.provider != "home_assistant":
            raise HomeAssistantOAuthServiceError("OAuth credential provider is invalid")
        self._resolve_token(record)
        return record

    def _store_token(
        self, config: HomeAssistantOAuthConfig, token: OAuthToken, *, operation: str
    ) -> OAuthCredentialRecord:
        payload = {
            "schema_version": _TOKEN_SCHEMA_VERSION,
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "scopes": list(token.scopes),
        }
        try:
            reference = self.secret_store.put(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        except Exception as error:  # noqa: BLE001 - secret store boundary is sanitized
            raise HomeAssistantOAuthServiceError("OAuth credential storage failed") from error
        record = OAuthCredentialRecord(
            provider="home_assistant",
            credential_ref=reference,
            expires_at=token.expires_at,
            scopes=token.scopes or config.scopes,
            refreshable=token.refresh_token is not None,
        )
        self._audit(operation, record)
        return record

    def _audit(self, event_type: str, record: OAuthCredentialRecord) -> None:
        if self.audit is not None:
            self.audit.append(
                event_type=event_type,
                actor="home_assistant_oauth",
                subject_id=record.credential_ref,
                payload={
                    "provider": record.provider,
                    "credential_ref": record.credential_ref,
                    "refreshable": record.refreshable,
                    "expires_at": (
                        record.expires_at.isoformat() if record.expires_at is not None else None
                    ),
                },
            )

    def _resolve_token(self, record: OAuthCredentialRecord) -> OAuthToken:
        try:
            payload = json.loads(self.secret_store.resolve(record.credential_ref))
            if not isinstance(payload, dict) or payload.get("schema_version") != (
                _TOKEN_SCHEMA_VERSION
            ):
                raise ValueError
            if set(payload) != {
                "schema_version",
                "access_token",
                "refresh_token",
                "expires_at",
                "scopes",
            }:
                raise ValueError
            access_token = payload["access_token"]
            refresh_token = payload["refresh_token"]
            expires_at = payload["expires_at"]
            scopes = payload["scopes"]
            if not isinstance(access_token, str) or not access_token:
                raise ValueError
            if refresh_token is not None and (
                not isinstance(refresh_token, str) or not refresh_token
            ):
                raise ValueError
            if expires_at is not None:
                expires_at = datetime.fromisoformat(expires_at)
                if expires_at.tzinfo is None:
                    raise ValueError
            if not isinstance(scopes, list) or not all(
                isinstance(scope, str) and scope for scope in scopes
            ):
                raise ValueError
            return OAuthToken(access_token, refresh_token, expires_at, tuple(scopes))
        except Exception as error:  # noqa: BLE001 - never expose token or storage details
            raise HomeAssistantOAuthServiceError("OAuth credential is invalid") from error

    def _delete(self, reference: str) -> None:
        try:
            self.secret_store.delete(reference)
        except Exception as error:  # noqa: BLE001 - no raw material in error
            raise HomeAssistantOAuthServiceError("OAuth credential could not be removed") from error


__all__ = [
    "HomeAssistantOAuthService",
    "HomeAssistantOAuthServiceError",
    "OAuthToken",
]
