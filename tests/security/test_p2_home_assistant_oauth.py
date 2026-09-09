from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.adapters.home_assistant.oauth import HomeAssistantOAuthConfig
from domoai.application.home_assistant_oauth import HomeAssistantOAuthService, OAuthToken
from domoai.runtime.clock import FixedClock
from domoai.security.secrets import FileSecretStore, SecretUnavailableError


class _OAuthFixture:
    async def exchange_code(self, config, *, code: str, code_verifier: str) -> OAuthToken:
        del config, code, code_verifier
        return OAuthToken(
            access_token="access-secret",
            refresh_token="refresh-secret",
            expires_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            scopes=("read",),
        )

    async def refresh(self, config, *, refresh_token: str) -> OAuthToken:
        del config, refresh_token
        return OAuthToken(
            access_token="rotated-access-secret",
            refresh_token="rotated-refresh-secret",
            expires_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
            scopes=("read",),
        )

    async def revoke(self, config, *, token: str) -> None:
        del config, token


class _AccessOnlyRefreshFixture(_OAuthFixture):
    async def refresh(self, config, *, refresh_token: str) -> OAuthToken:
        del config, refresh_token
        return OAuthToken(access_token="access-only-secret")


def _config() -> HomeAssistantOAuthConfig:
    return HomeAssistantOAuthConfig(
        authorization_url="https://accounts.example.test/authorize",
        token_url="https://accounts.example.test/token",
        revoke_url="https://accounts.example.test/revoke",
        client_id="domoai-client",
        redirect_uri="https://domoai.example.test/oauth/callback",
        scopes=("read",),
    )


@pytest.mark.asyncio
async def test_oauth_tokens_are_encrypted_and_refresh_rotates_reference(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    service = HomeAssistantOAuthService(
        store, _OAuthFixture(), clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    )
    started = service.begin(_config())
    first = await service.complete(_config(), state=started.state, code="authorization-code")
    rotated = await service.refresh(_config(), first)

    assert rotated.credential_ref != first.credential_ref
    assert "legacy-access-secret" not in repr(rotated)
    assert "rotated-access-secret" not in repr(rotated)
    raw = " ".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "secrets").glob("*.json")
    )
    assert "legacy-access-secret" not in raw
    assert "rotated-access-secret" not in raw
    with pytest.raises(SecretUnavailableError):
        store.resolve(first.credential_ref)

    await service.revoke(_config(), rotated)
    with pytest.raises(SecretUnavailableError):
        store.resolve(rotated.credential_ref)

    migrated = service.migrate(_config(), "legacy-access-secret")
    assert migrated.refreshable is False


@pytest.mark.asyncio
async def test_refresh_preserves_refresh_token_when_omitted(tmp_path) -> None:
    service = HomeAssistantOAuthService(
        FileSecretStore(tmp_path / "master.key"),
        _AccessOnlyRefreshFixture(),
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    started = service.begin(_config())
    first = await service.complete(_config(), state=started.state, code="authorization-code")
    rotated = await service.refresh(_config(), first)

    assert rotated.refreshable is True
