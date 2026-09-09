from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from domoai.application.connection_providers import (
    ConnectionProviderRegistry,
    FixtureConnectionProvider,
)
from domoai.domain.connections import (
    ConnectionProfile,
    SetupFieldDescriptor,
    SetupFieldType,
    SetupStep,
)
from domoai.management.api import create_management_app
from domoai.runtime.connection_ports import ConnectionProviderManifest


class _CatalogManager:
    def __init__(self) -> None:
        self.providers = ConnectionProviderRegistry()
        self.providers.register(
            FixtureConnectionProvider(
                ConnectionProviderManifest(
                    connector_id="simple_connector",
                    display_name="Simple Connector",
                    protocol="simple",
                    version="1.0",
                    supports_discovery=False,
                    steps=(
                        SetupStep(
                            step_id="connection",
                            title="Connection",
                            input_keys=("url", "token"),
                            secret_input_keys=("token",),
                            fields=(
                                SetupFieldDescriptor(
                                    key="url", label="URL", input_type=SetupFieldType.URL
                                ),
                                SetupFieldDescriptor(
                                    key="token",
                                    label="Token",
                                    input_type=SetupFieldType.PASSWORD,
                                    secret=True,
                                ),
                            ),
                        ),
                    ),
                ),
                adapter_factory=lambda: object(),
            )
        )
        self.received: list[tuple[str, str | None]] = []
        now = datetime.now(UTC)
        self.profile = ConnectionProfile(
            id=uuid4(),
            connector_id="simple_connector",
            unique_id="placeholder",
            endpoint={"url": "https://example"},
            options={},
            created_at=now,
            updated_at=now,
        )

    async def create(self, setup, *, credential_value: str | None = None, **kwargs):
        self.received.append((setup.candidate.candidate_id, credential_value))
        return self.profile


@pytest.mark.asyncio
async def test_generic_onboarding_supports_simple_endpoint_token_and_two_instances() -> None:
    manager = _CatalogManager()
    app = create_management_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        catalog = await client.get("/api/v1/connection-providers")
        first = await client.post(
            "/api/v1/connections",
            json={
                "connector_id": "simple_connector",
                "endpoint": {"url": "https://one.example"},
                "options": {},
                "credential": "first-secret",
            },
        )
        second = await client.post(
            "/api/v1/connections",
            json={
                "connector_id": "simple_connector",
                "endpoint": {"url": "https://two.example"},
                "options": {},
                "credential": "second-secret",
            },
        )
        invalid = await client.post(
            "/api/v1/connections",
            json={
                "connector_id": "simple_connector",
                "endpoint": {"token": "must-not-be-metadata"},
                "options": {},
            },
        )

    assert catalog.status_code == 200
    assert catalog.json()["providers"][0]["supports_manual_setup"] is True
    assert first.status_code == second.status_code == 201
    assert manager.received[0][0] != manager.received[1][0]
    assert [item[1] for item in manager.received] == ["first-secret", "second-secret"]
    assert "first-secret" not in first.text and "second-secret" not in second.text
    assert invalid.status_code == 400
    assert "must-not-be-metadata" not in invalid.text
