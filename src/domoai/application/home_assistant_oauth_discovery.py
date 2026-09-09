"""Bounded standard OAuth/OIDC metadata discovery for Home Assistant."""

from __future__ import annotations

from urllib.parse import urlparse

from domoai.adapters.home_assistant.oauth import (
    HomeAssistantOAuthClient,
    HomeAssistantOAuthError,
)
from domoai.domain.p2_connections import (
    OAuthMetadataDiscoveryResult,
    OAuthMetadataSource,
)


class HomeAssistantOAuthMetadataService:
    """Try only documented well-known locations, then ask for manual setup."""

    def __init__(
        self,
        client: HomeAssistantOAuthClient,
        *,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts <= 0 or max_attempts > 2:
            raise ValueError("OAuth metadata discovery attempts must be between 1 and 2")
        self.client = client
        self.max_attempts = max_attempts

    async def discover(
        self, base_url: str, *, metadata_url: str | None = None
    ) -> OAuthMetadataDiscoveryResult:
        root = _public_url(base_url)
        candidates = (
            ((metadata_url, _metadata_source(metadata_url)),)
            if metadata_url is not None
            else (
                (
                    f"{root}/.well-known/oauth-authorization-server",
                    OAuthMetadataSource.OAUTH_AUTHORIZATION_SERVER,
                ),
                (
                    f"{root}/.well-known/openid-configuration",
                    OAuthMetadataSource.OPENID_CONFIGURATION,
                ),
            )
        )
        for url, source in candidates[: self.max_attempts]:
            try:
                metadata = await self.client.fetch_metadata(url, source=source)
                return OAuthMetadataDiscoveryResult(metadata=metadata, manual_required=False)
            except HomeAssistantOAuthError:
                continue
        return OAuthMetadataDiscoveryResult(
            metadata=None,
            manual_required=True,
            diagnostic_code="oauth_metadata_unavailable",
            diagnostic_message="OAuth metadata is unavailable; enter the provider fields manually",
        )


def _public_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("OAuth provider URL is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("OAuth provider URL must not contain credentials")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _metadata_source(value: str) -> OAuthMetadataSource:
    if "openid-configuration" in value:
        return OAuthMetadataSource.OPENID_CONFIGURATION
    return OAuthMetadataSource.OAUTH_AUTHORIZATION_SERVER


__all__ = ["HomeAssistantOAuthMetadataService"]
