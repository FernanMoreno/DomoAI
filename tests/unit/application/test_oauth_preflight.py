from __future__ import annotations

import pytest

from domoai.application.oauth_preflight import OAuthDeploymentPreflight


def _env() -> dict[str, str]:
    return {
        "DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL": "https://ha.example.test/auth",
        "DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL": "https://ha.example.test/token",
        "DOMOAI_HOME_ASSISTANT_OAUTH_REVOKE_URL": "https://ha.example.test/revoke",
        "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID": "domoai-client",
        "DOMOAI_HOME_ASSISTANT_OAUTH_REDIRECT_URI": "http://127.0.0.1:8124/oauth/callback",
        "DOMOAI_HOME_ASSISTANT_OAUTH_SCOPES": "openid,homeassistant",
    }


def test_oauth_preflight_is_ready_locally_but_requires_external_registration() -> None:
    result = OAuthDeploymentPreflight.from_environment(_env())
    assert result.status == "manual_required"
    assert result.missing_fields == ()
    assert result.external_registration_required is True
    assert any(
        item.code == "oauth_client_registration_required" for item in result.diagnostics
    )
    assert result.configuration_fingerprint
    assert result.configuration_fingerprint.startswith("sha256:")


def test_oauth_preflight_reports_only_public_missing_field_names() -> None:
    result = OAuthDeploymentPreflight.from_environment(
        {"DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID": "client"}
    )
    assert result.status == "manual_required"
    assert "client_id" not in result.missing_fields
    assert "authorization_url" in result.missing_fields
    assert all("https://" not in item.message for item in result.diagnostics)


@pytest.mark.parametrize(
    "field",
    ("authorization_url", "token_url", "revoke_url"),
)
def test_oauth_preflight_rejects_remote_http_oauth_endpoints(field: str) -> None:
    values = _env()
    values[f"DOMOAI_HOME_ASSISTANT_OAUTH_{field.upper()}"] = (
        f"http://remote.example.test/{field}"
    )

    result = OAuthDeploymentPreflight.from_environment(values)

    assert result.status == "invalid"
    assert result.configuration_fingerprint is None


def test_oauth_preflight_accepts_explicit_loopback_http_endpoints() -> None:
    values = _env()
    values.update(
        {
            "DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL": "http://127.0.0.1:9000/authorize",
            "DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL": "http://localhost:9000/token",
            "DOMOAI_HOME_ASSISTANT_OAUTH_REVOKE_URL": "http://[::1]:9000/revoke",
        }
    )

    result = OAuthDeploymentPreflight.from_environment(values)

    assert result.status == "manual_required"
    assert result.configuration_fingerprint
