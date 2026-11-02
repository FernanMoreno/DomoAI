from __future__ import annotations

from pathlib import Path

import pytest
from tests.security.test_p2_home_assistant_oauth import _config, _OAuthFixture

from domoai.application.home_assistant_oauth import (
    HomeAssistantOAuthService,
    HomeAssistantOAuthServiceError,
)
from domoai.application.knx_secure import KnxSecureCredentialStore, KnxSecureError
from domoai.domain.coordination import LeaseScope
from domoai.security.secrets import FileSecretStore


@pytest.mark.asyncio
async def test_oauth_and_knx_pending_credentials_cannot_be_used_by_another_household(
    tmp_path: Path,
) -> None:
    scope = LeaseScope(tenant_id="tenant", household_id="home-a", deployment_id="edge")
    owner = FileSecretStore(tmp_path / "master.key", scope=scope)
    foreign = FileSecretStore(
        tmp_path / "master.key", scope=scope.model_copy(update={"household_id": "home-b"})
    )

    class CountedOAuth(_OAuthFixture):
        calls = 0

        async def refresh(self, config, *, refresh_token):
            self.calls += 1
            return await super().refresh(config, refresh_token=refresh_token)

        async def revoke(self, config, *, token):
            self.calls += 1
            return await super().revoke(config, token=token)

    own_service = HomeAssistantOAuthService(owner, _OAuthFixture())
    started = own_service.begin(_config())
    record = await own_service.complete(_config(), state=started.state, code="code")
    client = CountedOAuth()
    foreign_service = HomeAssistantOAuthService(foreign, client)
    for action in (foreign_service.refresh, foreign_service.revoke):
        with pytest.raises(HomeAssistantOAuthServiceError):
            await action(_config(), record)
    assert client.calls == 0
    assert own_service.validate_record(record) == record
    knx = KnxSecureCredentialStore(owner)
    reference = knx.put(user_password="knx-password")
    before = {p.name: p.read_bytes() for p in (tmp_path / "secrets").glob("*.json")}
    with pytest.raises(KnxSecureError):
        KnxSecureCredentialStore(foreign).rotate(reference, user_password="changed")
    assert before == {p.name: p.read_bytes() for p in (tmp_path / "secrets").glob("*.json")}
