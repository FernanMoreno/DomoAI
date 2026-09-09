from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from domoai.adapters.mqtt.config import MqttAdapterMapping, MqttDeviceMapping
from domoai.adapters.mqtt.discovery import MqttDiscoveryPreview
from domoai.application.connection_providers import (
    ConnectionProviderRegistry,
    ConnectionSetup,
    FixtureConnectionProvider,
)
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDraft,
    ConnectionProfile,
    SetupFieldDescriptor,
    SetupFieldType,
    SetupStep,
)
from domoai.domain.p1_completion import CommissioningMethod, CommissioningState
from domoai.management.api import create_management_app
from domoai.runtime.connection_ports import ConnectionProviderManifest


class StubManager:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.profile = ConnectionProfile(
            id=uuid4(),
            connector_id="fixture",
            unique_id="fixture:default",
            endpoint={"host": "fixture"},
            options={},
            credential_ref="secret:12345678-1234-5678-1234-567812345678",
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        self.received_credential: str | None = None
        self.setup: ConnectionSetup | None = None

    async def list(self) -> list[ConnectionProfile]:
        return [self.profile]

    async def get(self, connection_id: UUID) -> ConnectionProfile:
        return self.profile

    async def discover(self, connector_id: str) -> list[object]:
        return []

    async def start_setup(
        self, connector_id: str
    ) -> tuple[UUID, tuple[ConnectionCandidate, ...]]:
        candidate = ConnectionCandidate(
            connector_id=connector_id,
            candidate_id="fixture:default",
            display_name="Fixture",
            endpoint={"host": "fixture"},
        )
        return uuid4(), (candidate,)

    async def select_setup(self, session_id: UUID, candidate_id: str) -> ConnectionCandidate:
        return ConnectionCandidate(
            connector_id="fixture",
            candidate_id=candidate_id,
            display_name="Fixture",
            endpoint={"host": "fixture"},
        )

    async def setup_draft(self, session_id: UUID, **kwargs: object) -> ConnectionSetup:
        self.setup = ConnectionSetup(
            candidate=ConnectionCandidate(
                connector_id="fixture",
                candidate_id="fixture:default",
                display_name="Fixture",
                endpoint={"host": "fixture"},
            ),
            draft=ConnectionDraft(
                connector_id="fixture",
                unique_id="fixture:default",
                endpoint=kwargs["endpoint"],  # type: ignore[arg-type]
                options=kwargs["options"],  # type: ignore[arg-type]
            ),
        )
        return self.setup

    async def validate_setup(self, session_id: UUID) -> object:
        return {"valid": True}

    async def create_setup(
        self, session_id: UUID, *, activate: bool = True
    ) -> ConnectionProfile:
        return self.profile

    async def create(
        self, setup: object, *, credential_value: str | None = None
    ) -> ConnectionProfile:
        self.received_credential = credential_value
        return self.profile

    async def validate(self, setup: object) -> object:
        return {"valid": True}

    async def reconfigure(self, connection_id: UUID, **kwargs: object) -> ConnectionProfile:
        return self.profile

    async def activate(self, connection_id: UUID) -> ConnectionProfile:
        return self.profile

    async def disable(self, connection_id: UUID) -> bool:
        return True

    async def delete(self, connection_id: UUID) -> bool:
        return True

    async def reauth(self, connection_id: UUID, credential_value: str) -> ConnectionProfile:
        self.received_credential = credential_value
        return self.profile

    async def repair(self, connection_id: UUID) -> ConnectionProfile:
        return self.profile

    async def diagnose(self, connection_id: UUID) -> tuple[object, ...]:
        return ()


class StubMqttDiscovery:
    def __init__(self) -> None:
        mapping = MqttAdapterMapping(
            schema_version="v1",
            adapter_id="mqtt-discovery",
            devices=[
                MqttDeviceMapping(
                    source_id="mqtt:lamp",
                    type="light",
                    capabilities=[
                        {
                            "name": "power",
                            "state_topic": "home/lamp/state",
                            "command_topic": "home/lamp/set",
                            "commands": ["on", "off"],
                        }
                    ],
                )
            ],
        )
        self.preview_value = MqttDiscoveryPreview(
            adapter_id="mqtt-discovery",
            mapping=mapping,
            mapping_digest="a" * 64,
            source_topics=("homeassistant/light/lamp/config",),
            persistable=True,
        )
        self.confirmed: list[bool] = []

    async def preview(self) -> MqttDiscoveryPreview:
        return self.preview_value

    async def persist(self, preview: MqttDiscoveryPreview, *, confirmed: bool = False) -> str:
        if not confirmed:
            raise PermissionError("confirmation required")
        self.confirmed.append(confirmed)
        return "mqtt_mapping:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class StubMatterCommissioning:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.session = {
            "session_id": "matter-commissioning:1",
            "connection_id": "connection:1",
            "method": CommissioningMethod.MANUAL,
            "state": CommissioningState.AWAITING_CONFIRMATION,
            "deadline": now.replace(microsecond=0).isoformat(),
            "created_at": now.replace(microsecond=0).isoformat(),
            "confirmation_required": True,
            "cleanup_done": False,
        }
        self.codes: list[str] = []

    async def start(self, connection_id: str, **kwargs: object) -> object:
        assert connection_id == self.session["connection_id"]
        self.codes.append(str(kwargs["code"]))
        return self.session

    async def confirm(self, session_id: str) -> object:
        assert session_id == self.session["session_id"]
        self.session = {**self.session, "state": CommissioningState.COMPLETED, "cleanup_done": True}
        return self.session

    async def cleanup(self, session_id: str) -> object:
        assert session_id == self.session["session_id"]
        self.session = {**self.session, "state": CommissioningState.CLEANED, "cleanup_done": True}
        return self.session


@pytest.mark.asyncio
async def test_management_api_lists_and_creates_without_echoing_credential() -> None:
    manager = StubManager()
    app = create_management_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listed = await client.get("/api/v1/connections")
        created = await client.post(
            "/api/v1/connections",
            json={
                "connector_id": "fixture",
                "candidate_id": "fixture:default",
                "endpoint": {"host": "fixture"},
                "options": {},
                "credential": "raw-token-must-not-return",
            },
        )

    assert listed.status_code == 200
    assert created.status_code == 201
    assert manager.received_credential == "raw-token-must-not-return"
    assert "raw-token-must-not-return" not in created.text
    assert "raw-token-must-not-return" not in listed.text
    assert created.json()["connection"]["credential_configured"] is True


@pytest.mark.asyncio
async def test_management_api_exposes_secret_free_provider_catalog() -> None:
    manager = StubManager()
    manager.providers = ConnectionProviderRegistry()
    manager.providers.register(
        FixtureConnectionProvider(
            manifest=ConnectionProviderManifest(
                connector_id="fixture",
                display_name="Fixture",
                protocol="fixture",
                version="1.0",
                supports_discovery=False,
                steps=(
                    SetupStep(
                        step_id="credentials",
                        title="Credentials",
                        input_keys=("token",),
                        secret_input_keys=("token",),
                        fields=(
                            SetupFieldDescriptor(
                                key="token",
                                label="Access token",
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
    app = create_management_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/connection-providers")

    assert response.status_code == 200
    payload = response.json()
    assert payload["providers"][0]["connector_id"] == "fixture"
    assert payload["providers"][0]["steps"][0]["fields"][0]["secret"] is True
    assert "token-value" not in response.text


@pytest.mark.asyncio
async def test_management_api_exposes_control_operations_and_ui_states() -> None:
    manager = StubManager()
    app = create_management_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        connection_id = str(manager.profile.id)
        for method, path in (
            ("post", f"/api/v1/connections/{connection_id}/validate"),
            ("post", f"/api/v1/connections/{connection_id}/activate"),
            ("post", f"/api/v1/connections/{connection_id}/disable"),
            ("post", f"/api/v1/connections/{connection_id}/repair"),
            ("get", f"/api/v1/connections/{connection_id}/diagnostics"),
        ):
            response = await getattr(client, method)(path)
            assert response.status_code == 200
        reauth = await client.post(
            f"/api/v1/connections/{connection_id}/reauth",
            json={"credential": "another-raw-token"},
        )
        deleted = await client.delete(f"/api/v1/connections/{connection_id}")
        ui = await client.get("/")

    assert reauth.status_code == 200
    assert deleted.status_code == 204
    assert ui.status_code == 200
    assert "first-install" in ui.text
    assert "reauth" in ui.text
    assert "diagnostics" in ui.text
    assert "another-raw-token" not in reauth.text


@pytest.mark.asyncio
async def test_management_api_exposes_explicit_onboarding_session_steps() -> None:
    manager = StubManager()
    app = create_management_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        started = await client.post(
            "/api/v1/connections/setup", json={"connector_id": "fixture"}
        )
        session_id = started.json()["session_id"]
        selected = await client.post(
            "/api/v1/connections/setup",
            json={
                "action": "select",
                "session_id": session_id,
                "candidate_id": "fixture:default",
            },
        )
        configured = await client.post(
            "/api/v1/connections/setup",
            json={
                "action": "setup",
                "session_id": session_id,
                "endpoint": {"host": "fixture"},
                "options": {},
                "credential": "session-secret",
            },
        )
        validated = await client.post(
            "/api/v1/connections/setup",
            json={"action": "validate", "session_id": session_id},
        )
        created = await client.post(
            "/api/v1/connections/setup",
            json={"action": "create", "session_id": session_id, "activate": False},
        )

    assert started.status_code == 201
    assert selected.status_code == 200
    assert configured.status_code == 200
    assert "session-secret" not in configured.text
    assert validated.status_code == 200
    assert created.status_code == 201


@pytest.mark.asyncio
async def test_management_api_rejects_cross_origin_mutations() -> None:
    app = create_management_app(StubManager())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/connections/discovery",
            json={"connector_id": "fixture"},
            headers={"Origin": "https://attacker.example"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_management_api_reconfigures_without_accepting_credentials() -> None:
    manager = StubManager()
    app = create_management_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch(
            f"/api/v1/connections/{manager.profile.id}",
            json={"endpoint": {"host": "new-host"}, "options": {}},
        )
    assert response.status_code == 200
    assert "credential_ref" not in response.text


@pytest.mark.asyncio
async def test_management_api_previews_and_explicitly_persists_mqtt_discovery() -> None:
    discovery = StubMqttDiscovery()
    app = create_management_app(StubManager(), mqtt_discovery=discovery)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        preview_response = await client.post("/api/v1/mqtt/discovery/preview")
        unconfirmed_response = await client.post(
            "/api/v1/mqtt/discovery/persist",
            json={"preview": preview_response.json(), "confirmed": False},
        )
        persisted_response = await client.post(
            "/api/v1/mqtt/discovery/persist",
            json={"preview": preview_response.json(), "confirmed": True},
        )

    assert preview_response.status_code == 200
    assert preview_response.json()["persistable"] is True
    assert unconfirmed_response.status_code == 403
    assert persisted_response.status_code == 201
    assert persisted_response.json() == {
        "mapping_ref": "mqtt_mapping:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
    assert discovery.confirmed == [True]


@pytest.mark.asyncio
async def test_management_api_exposes_confirmed_matter_commissioning_without_code_echo() -> None:
    commissioning = StubMatterCommissioning()
    app = create_management_app(StubManager(), matter_commissioning=commissioning)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        started = await client.post(
            "/api/v1/matter/commissioning/start",
            json={
                "connection_id": "connection:1",
                "method": "manual",
                "code": "12345678",
                "can_commission": True,
                "prerequisites": {
                    "bluetooth_available": True,
                    "wifi_credentials": True,
                },
            },
        )
        confirmed = await client.post(
            "/api/v1/matter/commissioning/matter-commissioning:1/confirm"
        )
        cleaned = await client.post(
            "/api/v1/matter/commissioning/matter-commissioning:1/cleanup"
        )

    assert started.status_code == 200
    assert confirmed.status_code == 200
    assert cleaned.status_code == 200
    assert "12345678" not in started.text
    assert commissioning.codes == ["12345678"]


def test_non_loopback_management_requires_authentication_and_tls() -> None:
    manager = StubManager()
    with pytest.raises(ValueError, match="authentication"):
        create_management_app(manager, host="0.0.0.0", tls_enabled=True)
    with pytest.raises(ValueError, match="HTTPS"):
        create_management_app(manager, host="0.0.0.0", auth_token="operator", tls_enabled=False)


@pytest.mark.asyncio
async def test_management_api_authenticates_and_rate_limits_non_loopback_requests() -> None:
    manager = StubManager()
    app = create_management_app(
        manager,
        host="0.0.0.0",
        auth_token="operator-secret",
        tls_enabled=True,
        rate_limit=1,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        unauthorized = await client.get("/api/v1/connections")
        first = await client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer operator-secret"}
        )
        limited = await client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer operator-secret"}
        )

    assert unauthorized.status_code == 401
    assert first.status_code == 200
    assert limited.status_code == 429
