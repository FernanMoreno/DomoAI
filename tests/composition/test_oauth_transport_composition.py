from __future__ import annotations

import httpx
import pytest

from domoai.adapters.home_assistant.oauth import HomeAssistantOAuthClient, HomeAssistantOAuthConfig
from domoai.application.home_assistant_oauth_discovery import HomeAssistantOAuthMetadataService
from domoai.application.oauth_preflight import OAuthDeploymentPreflight


def _environment() -> dict[str, str]:
    return {
        "DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL": "https://provider.example/authorize",
        "DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL": "https://provider.example/token",
        "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID": "client",
        "DOMOAI_HOME_ASSISTANT_OAUTH_REDIRECT_URI": "https://gateway.example/callback",
        "DOMOAI_HOME_ASSISTANT_OAUTH_SCOPES": "read",
    }


@pytest.mark.composition
def test_preflight_and_adapter_reject_the_same_remote_http_endpoint() -> None:
    values = _environment()
    values["DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL"] = "http://provider.example/token"

    assert OAuthDeploymentPreflight.from_environment(values).status == "invalid"
    with pytest.raises(ValueError, match="HTTPS"):
        HomeAssistantOAuthConfig(
            authorization_url=values["DOMOAI_HOME_ASSISTANT_OAUTH_AUTHORIZATION_URL"],
            token_url=values["DOMOAI_HOME_ASSISTANT_OAUTH_TOKEN_URL"],
            client_id=values["DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID"],
            redirect_uri=values["DOMOAI_HOME_ASSISTANT_OAUTH_REDIRECT_URI"],
            scopes=("read",),
        )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_metadata_discovery_cannot_weaken_the_oauth_transport_policy() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://provider.example/authorize",
                "token_endpoint": "http://provider.example/token",
            },
        )

    result = await HomeAssistantOAuthMetadataService(
        HomeAssistantOAuthClient(transport=httpx.MockTransport(handler))
    ).discover("https://provider.example")

    assert result.manual_required is True
    assert result.metadata is None
