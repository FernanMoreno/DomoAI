"""External Home Assistant OAuth Authorization Code + PKCE client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import Field, model_validator

from domoai.domain.models import StrictModel
from domoai.domain.p2_connections import OAuthMetadataSource, OAuthProviderMetadata
from domoai.runtime.clock import Clock, SystemClock
from domoai.security.oauth_urls import validate_oauth_url


class HomeAssistantOAuthError(RuntimeError):
    """Sanitized error raised by the external OAuth boundary."""


class HomeAssistantOAuthConfig(StrictModel):
    authorization_url: str = Field(min_length=1, max_length=2048)
    token_url: str = Field(min_length=1, max_length=2048)
    revoke_url: str | None = Field(default=None, max_length=2048)
    client_id: str = Field(min_length=1, max_length=256)
    redirect_uri: str = Field(min_length=1, max_length=2048)
    scopes: tuple[str, ...] = Field(default=(), max_length=64)
    timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def validate_config(self) -> HomeAssistantOAuthConfig:
        for field_name in ("authorization_url", "token_url", "revoke_url", "redirect_uri"):
            value = getattr(self, field_name)
            if value is None:
                continue
            validate_oauth_url(value, field_name)
        if len(set(self.scopes)) != len(self.scopes) or any(
            not scope.strip() for scope in self.scopes
        ):
            raise ValueError("OAuth scopes must be unique and non-empty")
        return self


@dataclass(frozen=True, repr=False)
class OAuthToken:
    """Resolved token payload kept inside the OAuth service boundary."""

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "OAuthToken("
            f"has_access_token={bool(self.access_token)}, "
            f"has_refresh_token={self.refresh_token is not None}, "
            f"expires_at={self.expires_at!r}, scopes={self.scopes!r})"
        )


class HomeAssistantOAuthClient:
    """Small injectable HTTP client for an external OAuth provider."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.transport = transport
        self.clock = clock or SystemClock()

    async def exchange_code(
        self,
        config: HomeAssistantOAuthConfig,
        *,
        code: str,
        code_verifier: str,
    ) -> OAuthToken:
        if not code.strip() or not code_verifier.strip():
            raise HomeAssistantOAuthError("OAuth authorization code is invalid")
        return await self._token_request(
            config,
            {
                "grant_type": "authorization_code",
                "client_id": config.client_id,
                "redirect_uri": config.redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
        )

    async def refresh(
        self,
        config: HomeAssistantOAuthConfig,
        *,
        refresh_token: str,
    ) -> OAuthToken:
        if not refresh_token.strip():
            raise HomeAssistantOAuthError("OAuth refresh token is invalid")
        return await self._token_request(
            config,
            {
                "grant_type": "refresh_token",
                "client_id": config.client_id,
                "refresh_token": refresh_token,
            },
        )

    async def revoke(self, config: HomeAssistantOAuthConfig, *, token: str) -> None:
        if config.revoke_url is None:
            raise HomeAssistantOAuthError("OAuth revocation is not configured")
        if not token.strip():
            raise HomeAssistantOAuthError("OAuth token is invalid")
        try:
            async with httpx.AsyncClient(
                timeout=config.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    config.revoke_url,
                    data={"client_id": config.client_id, "token": token},
                )
                if response.status_code not in {200, 204}:
                    raise HomeAssistantOAuthError("OAuth token revocation failed")
        except HomeAssistantOAuthError:
            raise
        except (httpx.HTTPError, TimeoutError) as error:
            raise HomeAssistantOAuthError("OAuth token revocation failed") from error

    async def fetch_metadata(
        self,
        metadata_url: str,
        *,
        source: OAuthMetadataSource,
        max_response_bytes: int = 256 * 1024,
    ) -> OAuthProviderMetadata:
        """Fetch one standard OAuth/OIDC metadata document without following redirects."""

        try:
            validate_oauth_url(metadata_url, "metadata_url")
        except ValueError as error:
            raise HomeAssistantOAuthError("OAuth metadata URL is invalid") from error
        try:
            async with httpx.AsyncClient(
                timeout=10.0, transport=self.transport, follow_redirects=False
            ) as client:
                response = await client.get(metadata_url)
                if response.status_code != 200:
                    raise HomeAssistantOAuthError("OAuth metadata endpoint is unavailable")
                content = response.content
                if len(content) > max_response_bytes:
                    raise HomeAssistantOAuthError("OAuth metadata response is too large")
                payload = response.json()
        except HomeAssistantOAuthError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            raise HomeAssistantOAuthError("OAuth metadata response is invalid") from error
        if not isinstance(payload, dict):
            raise HomeAssistantOAuthError("OAuth metadata response is invalid")
        authorization_url = payload.get("authorization_endpoint")
        token_url = payload.get("token_endpoint")
        revoke_url = payload.get("revocation_endpoint")
        issuer = payload.get("issuer")
        scopes = payload.get("scopes_supported", ())
        if not isinstance(authorization_url, str) or not isinstance(token_url, str):
            raise HomeAssistantOAuthError("OAuth metadata response is invalid")
        if issuer is not None and not isinstance(issuer, str):
            raise HomeAssistantOAuthError("OAuth metadata response is invalid")
        if revoke_url is not None and not isinstance(revoke_url, str):
            raise HomeAssistantOAuthError("OAuth metadata response is invalid")
        try:
            validate_oauth_url(authorization_url, "authorization_url")
            validate_oauth_url(token_url, "token_url")
            if revoke_url is not None:
                validate_oauth_url(revoke_url, "revoke_url")
            normalized_scopes = _metadata_scopes(scopes)
            return OAuthProviderMetadata(
                issuer=issuer,
                authorization_url=authorization_url,
                token_url=token_url,
                revoke_url=revoke_url,
                scopes=normalized_scopes,
                source=source,
            )
        except ValueError as error:
            raise HomeAssistantOAuthError("OAuth metadata response is invalid") from error

    async def _token_request(
        self, config: HomeAssistantOAuthConfig, payload: dict[str, str]
    ) -> OAuthToken:
        try:
            async with httpx.AsyncClient(
                timeout=config.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(config.token_url, data=payload)
                response.raise_for_status()
                body = response.json()
        except HomeAssistantOAuthError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            raise HomeAssistantOAuthError("OAuth token exchange failed") from error
        return self._parse_token(body)

    def _parse_token(self, payload: Any) -> OAuthToken:
        if not isinstance(payload, dict):
            raise HomeAssistantOAuthError("OAuth token response is invalid")
        if any(
            key not in {"access_token", "refresh_token", "expires_in", "scope"}
            for key in payload
        ):
            raise HomeAssistantOAuthError("OAuth token response is invalid")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        scope = payload.get("scope", ())
        if not isinstance(access_token, str) or not access_token:
            raise HomeAssistantOAuthError("OAuth token response is invalid")
        if refresh_token is not None and (
            not isinstance(refresh_token, str) or not refresh_token
        ):
            raise HomeAssistantOAuthError("OAuth token response is invalid")
        if expires_in is not None and (
            not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in < 0
        ):
            raise HomeAssistantOAuthError("OAuth token response is invalid")
        scopes = _scopes(scope)
        expires_at = (
            self.clock.now().astimezone(UTC) + timedelta(seconds=expires_in)
            if expires_in is not None
            else None
        )
        return OAuthToken(access_token, refresh_token, expires_at, scopes)


def _scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = tuple(item for item in value.split() if item)
    elif isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        values = tuple(value)
    elif value in (None, ()):
        values = ()
    else:
        raise HomeAssistantOAuthError("OAuth token response is invalid")
    if len(set(values)) != len(values):
        raise HomeAssistantOAuthError("OAuth token response is invalid")
    return values


def _metadata_scopes(value: Any) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError("metadata scopes are invalid")
    if len(set(value)) != len(value):
        raise ValueError("metadata scopes are invalid")
    return tuple(value)


__all__ = [
    "HomeAssistantOAuthClient",
    "HomeAssistantOAuthConfig",
    "HomeAssistantOAuthError",
    "OAuthToken",
]
