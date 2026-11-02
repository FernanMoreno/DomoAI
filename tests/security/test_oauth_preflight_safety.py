from __future__ import annotations

from domoai.application.oauth_preflight import OAuthDeploymentPreflight


def test_oauth_preflight_rejects_credentials_and_never_echoes_them() -> None:
    secret = "super-secret-client-secret"
    result = OAuthDeploymentPreflight.from_environment(
        {
            "DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL": "https://ha.example.test/auth",
            "DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL": "https://ha.example.test/token",
            "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID": "client",
            "DOMOAI_HOME_ASSISTANT_OAUTH_REDIRECT_URI": "https://app.example.test/callback",
            "DOMOAI_HOME_ASSISTANT_OAUTH_SCOPES": "read read",
            "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_SECRET": secret,
        }
    )
    serialized = result.model_dump_json()
    assert result.status == "invalid"
    assert "forbidden" in serialized or "duplicate" in serialized
    assert secret not in serialized


def test_oauth_preflight_rejects_malformed_and_credential_bearing_urls() -> None:
    result = OAuthDeploymentPreflight.from_environment(
        {
            "DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL": "ftp://ha.example.test/auth",
            "DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL": "https://user:pass@ha.example.test/token",
            "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID": "client",
            "DOMOAI_HOME_ASSISTANT_OAUTH_REDIRECT_URI": "http://127.0.0.1:8124/callback",
        }
    )
    assert result.status == "invalid"
    assert result.configuration_fingerprint is None


def test_oauth_preflight_rejects_remote_http_before_any_request() -> None:
    result = OAuthDeploymentPreflight.from_environment(
        {
            "DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL": "https://ha.example.test/auth",
            "DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL": "http://remote.example.test/token",
            "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID": "client",
            "DOMOAI_HOME_ASSISTANT_OAUTH_REDIRECT_URI": "https://app.example.test/callback",
            "DOMOAI_HOME_ASSISTANT_OAUTH_SCOPES": "read",
        }
    )

    assert result.status == "invalid"
    assert result.configuration_fingerprint is None
