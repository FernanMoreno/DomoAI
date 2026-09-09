from __future__ import annotations

import pytest

from domoai.config.settings import Settings


def test_p2_external_oauth_configuration_is_complete_and_secret_free() -> None:
    settings = Settings(
        home_assistant_oauth_authorization_url="https://accounts.example.test/authorize",
        home_assistant_oauth_token_url="https://accounts.example.test/token",
        home_assistant_oauth_client_id="domoai-client",
        home_assistant_oauth_redirect_uri="https://domoai.example.test/callback",
        home_assistant_oauth_scopes=("read", "config"),
        knx_secure_mode="data_secure",
        knx_secure_keyring_path="/run/secrets/house.knxkeys",
        knx_secure_credential_ref="secret:12345678-1234-1234-1234-123456789012",
    )

    assert settings.home_assistant_oauth_client_id == "domoai-client"
    assert settings.knx_secure_mode == "data_secure"
    assert "house.knxkeys" in repr(settings)
    assert "12345678-1234-1234-1234-123456789012" in repr(settings)


def test_p2_oauth_configuration_rejects_partial_setup() -> None:
    with pytest.raises(ValueError, match="OAuth"):
        Settings(home_assistant_oauth_client_id="domoai-client")
