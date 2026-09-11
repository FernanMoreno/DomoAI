from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from domoai.adapters.mqtt.config import MqttAdapterMapping, MqttDeviceMapping
from domoai.adapters.mqtt.discovery import MqttDiscoveryPreview
from domoai.application.authority import AuthorityPolicy
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
from domoai.domain.connector_recipes import ConnectorRecipe
from domoai.domain.models import AuthorityContext, PrincipalRole
from domoai.domain.p1_completion import CommissioningMethod, CommissioningState
from domoai.management.api import create_management_app
from domoai.runtime.connection_ports import ConnectionProviderManifest
from domoai.runtime.events import AuditLog


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
        self.create_calls = 0
        self.mutation_calls = 0
        self.audit = AuditLog()

    def _mark_mutation(self) -> None:
        self.mutation_calls += 1

    async def list(self) -> list[ConnectionProfile]:
        return [self.profile]

    async def get(self, connection_id: UUID) -> ConnectionProfile:
        return self.profile

    async def discover(self, connector_id: str) -> list[object]:
        return []

    async def start_setup(self, connector_id: str) -> tuple[UUID, tuple[ConnectionCandidate, ...]]:
        self._mark_mutation()
        candidate = ConnectionCandidate(
            connector_id=connector_id,
            candidate_id="fixture:default",
            display_name="Fixture",
            endpoint={"host": "fixture"},
        )
        return uuid4(), (candidate,)

    async def select_setup(self, session_id: UUID, candidate_id: str) -> ConnectionCandidate:
        self._mark_mutation()
        return ConnectionCandidate(
            connector_id="fixture",
            candidate_id=candidate_id,
            display_name="Fixture",
            endpoint={"host": "fixture"},
        )

    async def setup_draft(self, session_id: UUID, **kwargs: object) -> ConnectionSetup:
        self._mark_mutation()
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
        self._mark_mutation()
        return {"valid": True}

    async def create_setup(self, session_id: UUID, *, activate: bool = True) -> ConnectionProfile:
        self._mark_mutation()
        return self.profile

    async def create(
        self, setup: object, *, credential_value: str | None = None
    ) -> ConnectionProfile:
        self._mark_mutation()
        self.create_calls += 1
        self.received_credential = credential_value
        return self.profile

    async def validate(self, setup: object) -> object:
        return {"valid": True}

    async def reconfigure(self, connection_id: UUID, **kwargs: object) -> ConnectionProfile:
        self._mark_mutation()
        return self.profile

    async def activate(self, connection_id: UUID) -> ConnectionProfile:
        self._mark_mutation()
        return self.profile

    async def disable(self, connection_id: UUID) -> bool:
        self._mark_mutation()
        return True

    async def delete(self, connection_id: UUID) -> bool:
        self._mark_mutation()
        return True

    async def reauth(self, connection_id: UUID, credential_value: str) -> ConnectionProfile:
        self._mark_mutation()
        self.received_credential = credential_value
        return self.profile

    async def repair(self, connection_id: UUID) -> ConnectionProfile:
        self._mark_mutation()
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
async def test_management_api_exposes_recipe_transport_and_discovery_metadata() -> None:
    manager = StubManager()
    manager.providers = ConnectionProviderRegistry()
    recipe = ConnectorRecipe.model_validate(
        {
            "connector_id": "fixture_recipe",
            "display_name": "Fixture Recipe",
            "transport_family": "mqtt",
            "discovery_strategies": ["manual", "mqtt_discovery"],
            "auth_schemes": ["token"],
            "discovery_options": {"topic": "fixture/discovery"},
            "capabilities": [
                {
                    "capability_id": "power",
                    "kind": "boolean",
                    "state_topic": "fixture/{device_id}/state",
                }
            ],
        }
    )
    manager.providers.register(
        FixtureConnectionProvider(
            manifest=ConnectionProviderManifest(
                connector_id="fixture_recipe",
                display_name="Fixture Recipe",
                protocol="mqtt",
                version="1.0",
                recipe=recipe,
            ),
            adapter_factory=lambda: object(),
        )
    )

    app = create_management_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/connection-providers")

    payload = response.json()
    exposed = payload["providers"][0]["recipe"]
    assert exposed["transport_family"] == "mqtt"
    assert exposed["discovery_strategies"] == ["manual", "mqtt_discovery"]
    assert exposed["auth_schemes"] == ["token"]
    assert exposed["discovery_options"] == {"topic": "fixture/discovery"}
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
        started = await client.post("/api/v1/connections/setup", json={"connector_id": "fixture"})
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
        confirmed = await client.post("/api/v1/matter/commissioning/matter-commissioning:1/confirm")
        cleaned = await client.post("/api/v1/matter/commissioning/matter-commissioning:1/cleanup")

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


def _management_token(
    *,
    token: str,
    scopes: list[str],
    tenant_id: str = "tenant-a",
    household_id: str = "home-a",
) -> AccessToken:
    authority = AuthorityContext(
        tenant_id=tenant_id,
        household_id=household_id,
        household_ids=[household_id],
        principal_id="management-client",
        roles=[
            PrincipalRole.OWNER
            if "manage" in scopes
            else (
                PrincipalRole.OPERATOR
                if {"mutate", "home:write"}.intersection(scopes)
                else PrincipalRole.VIEWER
            )
        ],
    )
    return AccessToken(
        token=token,
        client_id="management-client",
        scopes=scopes,
        claims={"authority": authority.model_dump(mode="json")},
    )


_MUTATING_MANAGEMENT_ROUTES = (
    ("POST", "/api/v1/connections"),
    ("POST", "/api/v1/connections/setup"),
    ("POST", "/api/v1/legacy/import"),
    ("POST", "/api/v1/mqtt/discovery/persist"),
    ("POST", "/api/v1/knx/ets/persist"),
    ("POST", "/api/v1/knx/secure/rotate"),
    ("POST", "/api/v1/home-assistant/oauth/start"),
    ("POST", "/api/v1/home-assistant/oauth/callback"),
    ("POST", "/api/v1/home-assistant/oauth/refresh"),
    ("POST", "/api/v1/home-assistant/oauth/revoke"),
    ("POST", "/api/v1/home-assistant/oauth/migrate"),
    ("POST", "/api/v1/home-assistant/oauth/reauth"),
    ("POST", "/api/v1/matter/commissioning/start"),
    ("POST", "/api/v1/matter/commissioning/session-1/confirm"),
    (
        "POST",
        "/api/v1/connections/11111111-1111-1111-1111-111111111111/repairs/action-1",
    ),
    ("PATCH", "/api/v1/connections/11111111-1111-1111-1111-111111111111"),
    ("POST", "/api/v1/connections/11111111-1111-1111-1111-111111111111/activate"),
    ("DELETE", "/api/v1/connections/11111111-1111-1111-1111-111111111111"),
)


@pytest.mark.parametrize(
    ("scopes", "role"),
    ((["read"], PrincipalRole.VIEWER), (["plan"], PrincipalRole.PLANNER)),
)
@pytest.mark.parametrize(("method", "path"), _MUTATING_MANAGEMENT_ROUTES)
@pytest.mark.asyncio
async def test_insufficient_management_authority_is_denied_for_every_mutation(
    scopes: list[str], role: PrincipalRole, method: str, path: str
) -> None:
    manager = StubManager()
    token = AccessToken(
        token=f"insufficient-{role.value}-{method}-{path}",
        client_id="management-client",
        scopes=scopes,
        claims={
            "authority": AuthorityContext(
                tenant_id="tenant-a",
                household_id="home-a",
                household_ids=["home-a"],
                principal_id="management-client",
                roles=[role],
            ).model_dump(mode="json")
        },
    )

    async def verify(value: str) -> AccessToken | None:
        return token if value == token.token else None

    app = create_management_app(
        manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        response = await client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token.token}"},
        )

    assert response.status_code == 403, f"{method} {path} accepted insufficient authority"
    assert manager.mutation_calls == 0
    assert manager.audit.events[-1].event_type == "management_authorization_rejected"
    assert token.token not in manager.audit.events[-1].model_dump_json()


@pytest.mark.asyncio
async def test_read_only_management_token_is_denied_before_mutation_handler() -> None:
    manager = StubManager()
    token = _management_token(token="read-only-secret", scopes=["read"])

    async def verify(value: str) -> AccessToken | None:
        return token if value == token.token else None

    app = create_management_app(
        manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        response = await client.post(
            "/api/v1/connections",
            json={"connector_id": "fixture", "endpoint": {}, "options": {}},
            headers={"Authorization": "Bearer read-only-secret"},
        )

    assert response.status_code == 403
    assert manager.create_calls == 0


@pytest.mark.asyncio
async def test_management_rejects_verified_token_from_wrong_tenant() -> None:
    manager = StubManager()
    token = _management_token(
        token="wrong-tenant-secret",
        scopes=["mutate"],
        tenant_id="tenant-b",
    )

    async def verify(value: str) -> AccessToken | None:
        return token if value == token.token else None

    app = create_management_app(
        manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        response = await client.get(
            "/api/v1/connections",
            headers={"Authorization": "Bearer wrong-tenant-secret"},
        )

    assert response.status_code == 403
    assert manager.create_calls == 0


@pytest.mark.asyncio
async def test_management_rejects_verified_token_from_wrong_household() -> None:
    manager = StubManager()
    token = _management_token(
        token="wrong-household-secret",
        scopes=["read"],
        household_id="home-b",
    )

    async def verify(value: str) -> AccessToken | None:
        return token if value == token.token else None

    app = create_management_app(
        manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        response = await client.get(
            "/api/v1/connections",
            headers={"Authorization": "Bearer wrong-household-secret"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_management_authorization_audit_never_contains_bearer() -> None:
    manager = StubManager()
    secret = "audit-management-secret"
    token = _management_token(token=secret, scopes=["read"])

    async def verify(value: str) -> AccessToken | None:
        return token if value == secret else None

    app = create_management_app(
        manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        response = await client.post(
            "/api/v1/connections",
            json={"connector_id": "fixture", "endpoint": {}, "options": {}},
            headers={"Authorization": f"Bearer {secret}"},
        )

    assert response.status_code == 403
    assert manager.audit.events[-1].event_type == "management_authorization_rejected"
    assert secret not in manager.audit.events[-1].model_dump_json()


@pytest.mark.asyncio
async def test_read_token_can_access_read_route_but_explicit_owner_can_manage() -> None:
    read_manager = StubManager()
    read_token = _management_token(token="read-secret", scopes=["read"])

    async def verify_read(value: str) -> AccessToken | None:
        return read_token if value == read_token.token else None

    read_app = create_management_app(
        read_manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify_read,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    read_transport = httpx.ASGITransport(app=read_app)
    async with httpx.AsyncClient(
        transport=read_transport, base_url="https://gateway.test"
    ) as client:
        read_response = await client.get(
            "/api/v1/connections",
            headers={"Authorization": "Bearer read-secret"},
        )

    operator_manager = StubManager()
    operator_token = _management_token(token="operator-secret", scopes=["manage"])

    async def verify_operator(value: str) -> AccessToken | None:
        return operator_token if value == operator_token.token else None

    operator_app = create_management_app(
        operator_manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify_operator,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    operator_transport = httpx.ASGITransport(app=operator_app)
    async with httpx.AsyncClient(
        transport=operator_transport, base_url="https://gateway.test"
    ) as client:
        mutate_response = await client.post(
            "/api/v1/connections",
            json={"connector_id": "fixture", "endpoint": {}, "options": {}},
            headers={"Authorization": "Bearer operator-secret"},
        )

    assert read_response.status_code == 200
    assert mutate_response.status_code == 201
    assert operator_manager.create_calls == 1
    assert operator_manager.audit.events[-1].event_type == "management_request_authorized"
    assert "operator-secret" not in operator_manager.audit.events[-1].model_dump_json()


@pytest.mark.asyncio
async def test_read_token_can_use_explicit_read_only_post_route() -> None:
    manager = StubManager()
    token = _management_token(token="read-discovery-secret", scopes=["read"])

    async def verify(value: str) -> AccessToken | None:
        return token if value == token.token else None

    app = create_management_app(
        manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        response = await client.post(
            "/api/v1/connections/discovery",
            json={"connector_id": "fixture"},
            headers={"Authorization": "Bearer read-discovery-secret"},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_read_token_is_denied_on_unknown_unsafe_route() -> None:
    manager = StubManager()
    token = _management_token(token="read-unknown-secret", scopes=["read"])

    async def verify(value: str) -> AccessToken | None:
        return token if value == token.token else None

    app = create_management_app(
        manager,
        host="0.0.0.0",
        tls_enabled=True,
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
        response = await client.post(
            "/api/v1/unknown-operation",
            headers={"Authorization": "Bearer read-unknown-secret"},
        )

    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path"), _MUTATING_MANAGEMENT_ROUTES)
@pytest.mark.parametrize("scopes", [["mutate"], ["home:write"]])
@pytest.mark.asyncio
async def test_operating_scope_never_grants_connection_administration(
    method: str,
    path: str,
    scopes: list[str],
) -> None:
    manager = StubManager()
    token = _management_token(token="operating-only", scopes=scopes)

    async def verify(value: str) -> AccessToken | None:
        return token if value == token.token else None

    app = create_management_app(
        manager, auth_verifier=verify, authority_policy=AuthorityPolicy("tenant-a", "home-a")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.request(
            method, path, headers={"Authorization": "Bearer operating-only"}
        )
    assert response.status_code == 403
    assert manager.mutation_calls == 0


@pytest.mark.parametrize("restriction", ["device_ids", "area_ids", "capabilities"])
@pytest.mark.asyncio
async def test_manage_does_not_erase_resource_acl(restriction: str) -> None:
    manager = StubManager()
    authority = AuthorityContext(
        tenant_id="tenant-a",
        household_id="home-a",
        household_ids=["home-a"],
        principal_id="owner",
        roles=[PrincipalRole.OWNER],
    ).model_copy(update={restriction: ["limited"]})
    token = AccessToken(
        token="limited-manage",
        client_id="owner",
        scopes=["manage"],
        claims={"authority": authority.model_dump(mode="json")},
    )

    async def verify(_value: str) -> AccessToken | None:
        return token

    app = create_management_app(
        manager, auth_verifier=verify, authority_policy=AuthorityPolicy("tenant-a", "home-a")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            f"/api/v1/connections/{manager.profile.id}/disable",
            headers={"Authorization": "Bearer limited-manage"},
        )
    assert response.status_code == 403
    assert manager.mutation_calls == 0


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"authority": {}},
        {"authority": None},
        {"authority": {"roles": ["service"]}},
        {"authority": {"roles": ["invalid"]}},
    ],
)
@pytest.mark.asyncio
async def test_manage_requires_complete_explicit_authority(claims: dict[str, object]) -> None:
    manager = StubManager()
    token = AccessToken(token="incomplete", client_id="owner", scopes=["manage"], claims=claims)

    async def verify(_value: str) -> AccessToken | None:
        return token

    app = create_management_app(manager, auth_verifier=verify, authority_policy=AuthorityPolicy())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            f"/api/v1/connections/{manager.profile.id}/disable",
            headers={"Authorization": "Bearer incomplete"},
        )
    assert response.status_code == 403
    assert manager.mutation_calls == 0


@pytest.mark.asyncio
async def test_repair_route_uuid_must_match_plan_before_execution() -> None:
    from domoai.domain.repairs import RepairPlan

    class RepairFixture:
        calls = 0

        async def execute(self, *_args: object, **_kwargs: object) -> dict[str, bool]:
            self.calls += 1
            return {"done": True}

    repair = RepairFixture()
    manager = StubManager()
    app = create_management_app(manager, repair_engine=repair)
    plan = RepairPlan(
        connection_id=str(uuid4()),
        diagnostics_digest="a" * 64,
        actions=(),
        created_at=datetime.now(UTC),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            f"/api/v1/connections/{manager.profile.id}/repairs/retry",
            json={"plan": plan.model_dump(mode="json")},
        )
    assert response.status_code == 400
    assert repair.calls == 0


@pytest.mark.asyncio
async def test_oauth_reauth_checks_connection_before_resolving_record() -> None:
    from domoai.application.connection_manager import ConnectionManagerError

    class UnknownConnection(StubManager):
        async def get(self, connection_id: UUID) -> ConnectionProfile:
            raise ConnectionManagerError("not_found", "connection was not found")

        async def reauth_ref(self, connection_id: UUID, reference: str) -> ConnectionProfile:
            self._mark_mutation()
            return self.profile

    class OAuthFixture:
        resolved = 0

        def validate_record(self, _record: object) -> None:
            self.resolved += 1

    manager, oauth = UnknownConnection(), OAuthFixture()
    app = create_management_app(manager, home_assistant_oauth=oauth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/v1/home-assistant/oauth/reauth",
            json={
                "connection_id": str(uuid4()),
                "record": {
                    "provider": "home_assistant",
                    "credential_ref": manager.profile.credential_ref,
                    "refreshable": False,
                    "scopes": [],
                },
            },
        )
    assert response.status_code == 404
    assert oauth.resolved == 0
    assert manager.mutation_calls == 0


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"scopes": ["*"]}, 403),
        ({"scopes": ["manage"], "roles": ["owner"]}, 403),
        (
            {
                "scopes": ["manage"],
                "roles": ["owner"],
                "tenant_id": "tenant-a",
                "household_ids": ["home-a"],
            },
            200,
        ),
        (
            {
                "scopes": ["manage"],
                "roles": ["operator"],
                "tenant_id": "tenant-a",
                "household_ids": ["home-a"],
            },
            403,
        ),
        (
            {
                "scopes": ["manage"],
                "roles": ["owner"],
                "tenant_id": "tenant-a",
                "household_ids": ["home-a"],
                "enabled": False,
            },
            401,
        ),
    ],
)
@pytest.mark.asyncio
async def test_token_file_must_declare_administrative_authority_before_verification(
    tmp_path,
    metadata: dict[str, object],
    expected: int,
) -> None:
    import hashlib
    import json

    from domoai.mcp.auth import StaticBearerTokenVerifier
    from domoai.mcp.token_lifecycle import TokenFileManager

    token_file = tmp_path / "clients.json"
    token_file.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "client",
                        "token_hash": hashlib.sha256(b"proof").hexdigest(),
                        **metadata,
                    }
                ]
            }
        )
    )
    verifier = StaticBearerTokenVerifier.from_file(token_file)
    manager = StubManager()
    app = create_management_app(
        manager,
        auth_verifier=verifier.verify_token,
        authority_policy=AuthorityPolicy(
            str(metadata.get("tenant_id", "default")),
            "home-a" if "household_ids" in metadata else "default",
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            f"/api/v1/connections/{manager.profile.id}/disable",
            headers={"Authorization": "Bearer proof"},
        )
        read = await client.get("/api/v1/connections", headers={"Authorization": "Bearer proof"})
        TokenFileManager(token_file).rotate("unrelated-client", scopes=["read"])
        verifier.reload()
        after_rewrite = await client.post(
            f"/api/v1/connections/{manager.profile.id}/disable",
            headers={"Authorization": "Bearer proof"},
        )
    assert after_rewrite.status_code == expected
    assert response.status_code == expected
    assert manager.mutation_calls == (2 if expected == 200 else 0)
    assert read.status_code == (401 if expected == 401 else 200)
