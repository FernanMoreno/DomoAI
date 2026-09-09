from __future__ import annotations

import httpx
import pytest

from domoai.adapters.home_assistant.oauth import HomeAssistantOAuthClient
from domoai.application.home_assistant_oauth_discovery import HomeAssistantOAuthMetadataService


@pytest.mark.asyncio
async def test_discovery_rejects_embedded_credentials_and_returns_manual_fallback() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://user:password@example/authorize",
                "token_endpoint": "https://example/token",
            },
        )

    result = await HomeAssistantOAuthMetadataService(
        HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    ).discover("https://example")

    assert result.manual_required is True
    assert "password" not in result.model_dump_json().casefold()


@pytest.mark.asyncio
async def test_discovery_rejects_oversized_metadata_without_following_redirects() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"{" + b"a" * (256 * 1024) + b"}")

    result = await HomeAssistantOAuthMetadataService(
        HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    ).discover("https://example")

    assert result.manual_required is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_discovery_rejects_credential_bearing_metadata_url_before_request() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    result = await HomeAssistantOAuthMetadataService(
        HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    ).discover(
        "https://example",
        metadata_url="https://user:password@example/.well-known/metadata",
    )

    assert result.manual_required is True
    assert calls == []
