from __future__ import annotations

import httpx
import pytest

from domoai.adapters.home_assistant.oauth import HomeAssistantOAuthClient, HomeAssistantOAuthConfig
from domoai.application.home_assistant_oauth import HomeAssistantOAuthService, OAuthToken
from domoai.application.home_assistant_oauth_discovery import HomeAssistantOAuthMetadataService
from domoai.management.api import create_management_app
from domoai.security.secrets import FileSecretStore


def test_oauth_migration_returns_reference_only_record(tmp_path) -> None:
    config = HomeAssistantOAuthConfig(
        authorization_url="https://accounts.example.test/authorize",
        token_url="https://accounts.example.test/token",
        client_id="domoai-client",
        redirect_uri="https://domoai.example.test/oauth/callback",
        scopes=("read",),
    )
    record = HomeAssistantOAuthService(
        FileSecretStore(tmp_path / "master.key"), object()
    ).migrate(config, "legacy-long-lived-token")

    payload = record.model_dump_json()
    assert record.credential_ref.startswith("secret:")
    assert "legacy-long-lived-token" not in payload
    assert "access_token" not in payload


class _OAuthFixture:
    async def exchange_code(self, config, *, code: str, code_verifier: str) -> OAuthToken:
        del config, code, code_verifier
        return OAuthToken(access_token="callback-access", refresh_token="callback-refresh")


@pytest.mark.asyncio
async def test_oauth_routes_return_only_opaque_credential_metadata(tmp_path) -> None:
    config = HomeAssistantOAuthConfig(
        authorization_url="https://accounts.example.test/authorize",
        token_url="https://accounts.example.test/token",
        client_id="domoai-client",
        redirect_uri="https://domoai.example.test/oauth/callback",
        scopes=("read",),
    )
    service = HomeAssistantOAuthService(FileSecretStore(tmp_path / "master.key"), _OAuthFixture())
    app = create_management_app(
        object(), home_assistant_oauth=service, home_assistant_oauth_config=config
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        started = await client.post("/api/v1/home-assistant/oauth/start", json={})
        completed = await client.post(
            "/api/v1/home-assistant/oauth/callback",
            json={"state": started.json()["state"], "code": "authorization-code"},
        )

    assert started.status_code == 200
    assert completed.status_code == 201
    assert "callback-access" not in completed.text
    assert "callback-refresh" not in completed.text
    assert completed.json()["credential_ref"].startswith("secret:")


@pytest.mark.asyncio
async def test_oauth_callback_uses_configuration_bound_to_state(tmp_path) -> None:
    config = HomeAssistantOAuthConfig(
        authorization_url="https://accounts.example.test/authorize",
        token_url="https://accounts.example.test/token",
        client_id="domoai-client",
        redirect_uri="https://domoai.example.test/oauth/callback",
        scopes=("read",),
    )
    service = HomeAssistantOAuthService(FileSecretStore(tmp_path / "master.key"), _OAuthFixture())
    started = service.begin(config)

    completed = await service.complete(
        None,
        state=started.state,
        code="authorization-code",
    )

    assert completed.provider == "home_assistant"


@pytest.mark.asyncio
async def test_oauth_api_discovers_endpoints_and_callback_needs_only_state(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(404)
        assert request.url.path == "/.well-known/openid-configuration"
        return httpx.Response(
            200,
            json={
                "issuer": "https://accounts.example.test",
                "authorization_endpoint": "https://accounts.example.test/authorize",
                "token_endpoint": "https://accounts.example.test/token",
                "scopes_supported": ["read"],
            },
        )

    oauth_client = HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    discovery = HomeAssistantOAuthMetadataService(oauth_client)
    oauth_service = HomeAssistantOAuthService(
        FileSecretStore(tmp_path / "master.key"), _OAuthFixture()
    )
    app = create_management_app(
        object(),
        home_assistant_oauth=oauth_service,
        home_assistant_oauth_discovery=discovery,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        discovered = await client.post(
            "/api/v1/home-assistant/oauth/discover",
            json={"base_url": "https://accounts.example.test"},
        )
        started = await client.post(
            "/api/v1/home-assistant/oauth/start",
            json={
                "base_url": "https://accounts.example.test",
                "client_id": "domoai-client",
                "redirect_uri": "https://domoai.example.test/oauth/callback",
            },
        )
        completed = await client.post(
            "/api/v1/home-assistant/oauth/callback",
            json={"state": started.json()["state"], "code": "authorization-code"},
        )

    assert discovered.status_code == 200
    assert discovered.json()["metadata"]["token_url"].endswith("/token")
    assert started.status_code == 200
    assert completed.status_code == 201
