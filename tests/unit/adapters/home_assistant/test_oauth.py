from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from domoai.adapters.home_assistant.oauth import (
    HomeAssistantOAuthClient,
    HomeAssistantOAuthConfig,
    HomeAssistantOAuthError,
)
from domoai.application.home_assistant_oauth import HomeAssistantOAuthService
from domoai.runtime.clock import FixedClock
from domoai.security.secrets import FileSecretStore


def _config() -> HomeAssistantOAuthConfig:
    return HomeAssistantOAuthConfig(
        authorization_url="https://accounts.example.test/authorize",
        token_url="https://accounts.example.test/token",
        revoke_url="https://accounts.example.test/revoke",
        client_id="domoai-client",
        redirect_uri="https://domoai.example.test/oauth/callback",
        scopes=("config", "read"),
    )


@pytest.mark.parametrize(
    "field",
    ("authorization_url", "token_url", "revoke_url"),
)
def test_oauth_config_rejects_remote_http_oauth_endpoints(field: str) -> None:
    values = _config().model_dump()
    values[field] = f"http://remote.example.test/{field}"

    with pytest.raises(ValueError, match="OAuth"):
        HomeAssistantOAuthConfig(**values)


def test_oauth_config_accepts_explicit_loopback_http_endpoints() -> None:
    values = _config().model_dump()
    values.update(
        {
            "authorization_url": "http://127.0.0.1:9000/authorize",
            "token_url": "http://localhost:9000/token",
            "revoke_url": "http://[::1]:9000/revoke",
        }
    )

    assert HomeAssistantOAuthConfig(**values).token_url.startswith("http://")


@pytest.mark.asyncio
async def test_oauth_token_and_revoke_clients_explicitly_disable_redirects(monkeypatch) -> None:
    observed: list[object] = []
    real_client = httpx.AsyncClient

    class RecordingAsyncClient(real_client):
        def __init__(self, *args, **kwargs):
            observed.append(kwargs.get("follow_redirects", "missing"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingAsyncClient)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    client = HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    with pytest.raises(HomeAssistantOAuthError, match="token exchange"):
        await client.exchange_code(_config(), code="code", code_verifier="verifier")
    with pytest.raises(HomeAssistantOAuthError, match="revocation"):
        await client.revoke(_config(), token="token")

    assert observed == [False, False]


@pytest.mark.asyncio
async def test_oauth_client_exchanges_code_and_service_uses_pkce(tmp_path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
                "scope": "config read",
            },
        )

    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    client = HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    service = HomeAssistantOAuthService(
        FileSecretStore(tmp_path / "master.key"), client, clock=clock
    )
    started = service.begin(_config())
    query = parse_qs(urlparse(started.authorization_url).query)

    assert query["state"] == [started.state]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) > 30

    record = await service.complete(_config(), state=started.state, code="one-time-code")

    assert record.provider == "home_assistant"
    assert record.refreshable is True
    assert "access-secret" not in repr(record)
    assert requests[0].url.path == "/token"
    assert b"code_verifier" in requests[0].content
    assert b"access-secret" not in requests[0].content


@pytest.mark.asyncio
async def test_oauth_state_is_single_use_and_expiry_is_enforced(tmp_path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "access-secret"})

    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    service = HomeAssistantOAuthService(
        FileSecretStore(tmp_path / "master.key"),
        HomeAssistantOAuthClient(transport=httpx.MockTransport(handler)),
        clock=clock,
        state_ttl_seconds=60,
    )
    started = service.begin(_config())
    await service.complete(_config(), state=started.state, code="one-time-code")

    with pytest.raises(HomeAssistantOAuthError, match="state"):
        await service.complete(_config(), state=started.state, code="replayed")

    expired = service.begin(_config())
    clock.set(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))
    with pytest.raises(HomeAssistantOAuthError, match="expired"):
        await service.complete(_config(), state=expired.state, code="expired")


@pytest.mark.asyncio
async def test_oauth_client_rejects_token_payload_without_access_token() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "refresh-secret"})

    client = HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    with pytest.raises(HomeAssistantOAuthError, match="token response"):
        await client.exchange_code(_config(), code="code", code_verifier="verifier")
