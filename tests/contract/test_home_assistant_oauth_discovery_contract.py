from __future__ import annotations

import httpx
import pytest

from domoai.adapters.home_assistant.oauth import HomeAssistantOAuthClient
from domoai.application.home_assistant_oauth_discovery import HomeAssistantOAuthMetadataService
from domoai.domain.p2_connections import OAuthMetadataSource


@pytest.mark.asyncio
async def test_discovery_tries_standard_oauth_then_oidc_metadata() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "issuer": "https://ha.example",
                "authorization_endpoint": "https://ha.example/authorize",
                "token_endpoint": "https://ha.example/token",
                "revocation_endpoint": "https://ha.example/revoke",
                "scopes_supported": ["openid", "read"],
            },
        )

    service = HomeAssistantOAuthMetadataService(
        HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    )
    result = await service.discover("https://ha.example")

    assert result.metadata is not None
    assert result.metadata.source is OAuthMetadataSource.OPENID_CONFIGURATION
    assert result.metadata.scopes == ("openid", "read")
    assert calls == [
        "https://ha.example/.well-known/oauth-authorization-server",
        "https://ha.example/.well-known/openid-configuration",
    ]


@pytest.mark.asyncio
async def test_discovery_returns_safe_manual_fallback_on_unavailable_metadata() -> None:
    service = HomeAssistantOAuthMetadataService(
        HomeAssistantOAuthClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503))
        )
    )

    result = await service.discover("https://ha.example")

    assert result.metadata is None
    assert result.manual_required is True
    assert result.diagnostic_code == "oauth_metadata_unavailable"
    assert "503" not in (result.diagnostic_message or "")

