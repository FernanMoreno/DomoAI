from __future__ import annotations

import ssl
from collections.abc import AsyncIterator

import pytest

from domoai.adapters.home_assistant.connection import HomeAssistantConnectionProvider
from domoai.application.connection_providers import ConnectionSetup
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft, DiagnosticSeverity


class Resolver:
    def resolve(self, reference: str) -> str:
        assert reference == "env:HA_TOKEN"
        return "resolved-token"


class FailingResolver:
    def resolve(self, reference: str) -> str:
        raise PermissionError("token=must-not-leak")


class FakeClient:
    def __init__(self, base_url: str, token: str, *, healthy: bool = True) -> None:
        self.base_url = base_url
        self.token = token
        self.healthy = healthy

    async def health(self) -> bool:
        return self.healthy

    async def close(self) -> None:
        return None

    async def fetch_states(self) -> list[dict[str, object]]:
        return []

    async def fetch_entity_registry(self) -> list[dict[str, object]]:
        return []

    async def fetch_device_registry(self) -> list[dict[str, object]]:
        return []

    def subscribe_state_events(self) -> AsyncIterator[dict[str, object]]:
        async def events() -> AsyncIterator[dict[str, object]]:
            if False:
                yield {}

        return events()


class UnauthorizedClient(FakeClient):
    async def health(self) -> bool:
        raise PermissionError("token=must-not-leak")


def _setup(*, credential_ref: str | None = "env:HA_TOKEN") -> ConnectionSetup:
    candidate = ConnectionCandidate(
        connector_id="home_assistant",
        candidate_id="ha:house-1",
        display_name="House Assistant",
        endpoint={"url": "https://ha.local"},
        metadata={"source": "manual"},
    )
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="home_assistant",
            unique_id="ha:house-1",
            endpoint={"url": "https://ha.local"},
            options={},
            credential_ref=credential_ref,
        ),
    )


@pytest.mark.asyncio
async def test_home_assistant_provider_validates_and_builds_existing_adapter() -> None:
    provider = HomeAssistantConnectionProvider(
        secret_resolver=Resolver(),
        client_factory=FakeClient,
    )

    validation = await provider.validate(_setup())
    assert validation.valid
    adapter = await provider.build_adapter(_setup(), adapter_id="connection:ha-1")
    assert adapter.adapter_id == "connection:ha-1"
    assert adapter.provider.client.token == "resolved-token"


@pytest.mark.asyncio
async def test_manual_and_injected_mdns_discovery_are_secret_free() -> None:
    async def mdns() -> list[ConnectionCandidate]:
        return [
            ConnectionCandidate(
                connector_id="home_assistant",
                candidate_id="ha:mdns-1",
                display_name="Discovered HA",
                endpoint={"url": "http://ha.local:8123"},
                metadata={"source": "mdns"},
            )
        ]

    provider = HomeAssistantConnectionProvider(
        secret_resolver=Resolver(),
        client_factory=FakeClient,
        mdns_discover=mdns,
    )
    candidates = await provider.discover()
    manual = provider.manual_candidate("https://ha.local")
    assert candidates[0].metadata["source"] == "mdns"
    assert manual.candidate_id == "ha:https://ha.local"
    assert "resolved-token" not in repr(candidates + [manual])


@pytest.mark.asyncio
async def test_missing_credentials_and_timeout_are_actionable_without_secret_values() -> None:
    provider = HomeAssistantConnectionProvider(
        secret_resolver=Resolver(),
        client_factory=lambda url, token: FakeClient(url, token, healthy=False),
    )
    validation = await provider.validate(_setup(credential_ref=None))
    assert not validation.valid
    assert validation.diagnostics[0].severity is DiagnosticSeverity.ERROR
    assert "token" not in validation.diagnostics[0].message.casefold()

    diagnostics = await provider.diagnose(_setup())
    assert diagnostics[0].code == "unreachable"


@pytest.mark.asyncio
async def test_auth_and_tls_failures_have_stable_sanitized_codes() -> None:
    auth_provider = HomeAssistantConnectionProvider(
        secret_resolver=FailingResolver(),
        client_factory=FakeClient,
    )
    auth_result = await auth_provider.validate(_setup())
    assert auth_result.diagnostics[0].code == "authentication_failed"
    assert "must-not-leak" not in repr(auth_result)

    def tls_client(url: str, token: str) -> FakeClient:
        raise ssl.SSLError("certificate=must-not-leak")

    tls_provider = HomeAssistantConnectionProvider(
        secret_resolver=Resolver(),
        client_factory=tls_client,
    )
    tls_result = await tls_provider.validate(_setup())
    assert tls_result.diagnostics[0].code == "tls_certificate_invalid"
    assert "must-not-leak" not in repr(tls_result)

    unauthorized_provider = HomeAssistantConnectionProvider(
        secret_resolver=Resolver(),
        client_factory=UnauthorizedClient,
    )
    unauthorized_result = await unauthorized_provider.validate(_setup())
    assert unauthorized_result.diagnostics[0].code == "authentication_failed"
    assert "must-not-leak" not in repr(unauthorized_result)
