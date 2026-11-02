"""Public shell and protected household operations share the same HTTP guard."""

import httpx
import pytest
from tests.integration.test_management_api import StubManager, _management_token

from domoai.application.authority import AuthorityPolicy
from domoai.management.api import create_management_app


@pytest.mark.asyncio
async def test_public_shell_is_identical_and_never_verifies_supplied_bearer() -> None:
    async def unavailable_verifier(_token):
        raise AssertionError("a static public shell must not inspect credentials")

    app = create_management_app(
        object(),
        auth_verifier=unavailable_verifier,
        authority_policy=AuthorityPolicy(tenant_id="private-tenant", household_id="private-home"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        anonymous = await client.get("/")
        assert anonymous.status_code == 200
        authenticated = await client.get("/", headers={"Authorization": "Bearer fixture-secret"})
        head = await client.head("/", headers={"Authorization": "Bearer invalid"})
    assert authenticated.content == anonymous.content
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == anonymous.headers["content-length"]
    assert all(
        value not in anonymous.text
        for value in ["private-tenant", "private-home", "fixture-secret"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/"),
        ("OPTIONS", "/"),
        ("GET", "/api/v1/connections"),
        ("GET", "/api/v1/onboarding/status"),
        ("GET", "/not-the-shell"),
    ],
)
async def test_public_exception_never_extends_to_other_methods_or_routes(method, path) -> None:
    app = create_management_app(object(), auth_token="fixture")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.request(method, path)
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/api/v1/connections"])
async def test_public_and_unauthorized_requests_are_limited_and_never_cached(path) -> None:
    app = create_management_app(object(), auth_token="fixture", rate_limit=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        first = await client.get(path)
        second = await client.get(path)
    assert first.status_code == (200 if path == "/" else 401)
    assert second.status_code == 429
    for response in [first, second]:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["read", "claims", "foreign", "partial", "origin"])
async def test_onboarding_mutations_keep_a05_authority_before_manager(case) -> None:
    manager = StubManager()
    token = _management_token(token="fixture", scopes=["manage"])
    if case == "read":
        token = token.model_copy(update={"scopes": ["read"]})
    elif case == "claims":
        token = token.model_copy(update={"claims": {"authority": {"tenant_id": "tenant-a"}}})
    elif case in {"foreign", "partial"}:
        claims = dict(token.claims)
        authority = dict(claims["authority"])
        authority.update(
            {"tenant_id": "foreign"} if case == "foreign" else {"capabilities": ["brightness"]}
        )
        claims["authority"] = authority
        token = token.model_copy(update={"claims": claims})

    async def verify(_value):
        return token

    app = create_management_app(
        manager,
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    headers = {"Authorization": "Bearer fixture"}
    if case == "origin":
        headers["Origin"] = "https://foreign.test"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/v1/connections/setup",
            headers=headers,
            json={"action": "start", "connector_id": "fixture"},
        )
    assert response.status_code == 403
    assert manager.mutation_calls == 0
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["verifier", "manager"])
async def test_unexpected_failures_keep_sanitized_500_and_common_headers(boundary) -> None:
    class FailingManager:
        async def list(self):
            raise RuntimeError("private-manager-credential")

    async def verify(_value):
        if boundary == "verifier":
            raise RuntimeError("private-verifier-credential")
        return _management_token(token="fixture", scopes=["read"])

    app = create_management_app(
        FailingManager(),
        auth_verifier=verify,
        authority_policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        response = await client.get(
            "/api/v1/connections", headers={"Authorization": "Bearer fixture"}
        )
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("referrer-policy") == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers.get("content-security-policy", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("area,allowed", [("allowed", True), ("foreign", False), (None, False)])
@pytest.mark.parametrize("capability_acl", [[], ["not_declared"]])
async def test_area_scoped_state_read_rejects_unassigned_device_before_io(
    area, allowed, capability_acl
):
    from domoai.application.household_onboarding import HouseholdOnboardingService
    from domoai.application.state_service import StateService
    from domoai.domain.models import Device
    from domoai.mcp.resources import coverage_evidence_snapshot
    from domoai.runtime.registry import DeviceRegistry
    from domoai.runtime.state_store import StateStore

    class ObservedStateService(StateService):
        calls = 0

        async def get_with_diagnostics(self, *args, **kwargs):
            self.calls += 1
            return await super().get_with_diagnostics(*args, **kwargs)

    registry = DeviceRegistry()
    registry.load_persisted(
        [
            Device(
                id="fixture.sensor",
                type="sensor",
                name="Fixture",
                protocol="fixture",
                area_id=area,
                capabilities=[
                    dict(name="temperature", kind="number", readable=True, writable=False)
                ],
                source_refs=[dict(adapter_id="fixture", external_id="sensor")],
            )
        ]
    )
    state = ObservedStateService(StateStore())
    policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")
    service = HouseholdOnboardingService(
        registry=registry,
        state_service=state,
        manager=StubManager(),
        authority_policy=policy,
        coverage_projector=coverage_evidence_snapshot,
        membership=lambda: ("fixture",),
        readiness=lambda: [],
        public_url="http://localhost/mcp",
    )
    token = _management_token(token="fixture", scopes=["read"])
    token.claims["authority"]["area_ids"] = ["allowed"]
    token.claims["authority"]["capabilities"] = capability_acl

    async def verify(_value):
        return token

    app = create_management_app(
        service.manager,
        auth_verifier=verify,
        authority_policy=policy,
        household_onboarding=service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer fixture"},
    ) as client:
        inventory = await client.get("/api/v1/onboarding/inventory")
        response = await client.get("/api/v1/onboarding/state/fixture.sensor")
    assert inventory.status_code == 200
    assert len(inventory.json()["devices"]) == int(allowed)
    assert response.status_code == (200 if allowed else 403)
    assert state.calls == int(allowed and not capability_acl)
    if allowed and capability_acl:
        assert response.json()["diagnostics"] == ["capability_unavailable"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["devices", "capabilities", "coverage", "coverage-evidence"])
async def test_mcp_inventory_capability_acl_projects_without_changing_registry(resource):
    from tests.composition.test_scoped_area_inventory_composition import request_authority
    from tests.contract.test_effective_route_coverage import build_route_context, read_resource
    from tests.contract.test_scoped_area_inventory_contract import scoped_authority

    adapter, context, server = build_route_context((("a", True),), ("a",))
    canonical = [device.model_dump(mode="json") for device in context.registry.devices]
    authority = scoped_authority(devices=("home.light",)).model_copy(
        update={"capabilities": ["brightness"]}
    )
    with request_authority(authority):
        payload = await read_resource(server, resource)
    import json

    assert "temperature" not in json.dumps(payload)
    assert "brightness" in json.dumps(payload)
    assert [device.model_dump(mode="json") for device in context.registry.devices] == canonical
    assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [None, [], ["brightness"], ["temperature"]])
async def test_mcp_state_capability_acl_cannot_expand_when_selection_is_empty(selection):
    from datetime import UTC, datetime

    from tests.composition.test_scoped_area_inventory_composition import request_authority
    from tests.contract.test_effective_route_coverage import build_route_context
    from tests.contract.test_scoped_area_inventory_contract import scoped_authority
    from tests.contract.test_unified_mcp_contract import structured

    from domoai.domain.models import SourceRef, StateSnapshot, StateStatus

    adapter, context, server = build_route_context((("a", True),), ("a",))
    now = datetime.now(UTC)
    for capability, value in [("brightness", 50), ("temperature", 21)]:
        await context.state_service.state_store.save(
            StateSnapshot(
                device_id="home.light",
                capability=capability,
                value=value,
                observed_at=now,
                received_at=now,
                status=StateStatus.CURRENT,
                source_ref=SourceRef(adapter_id="a", external_id="home.light"),
            )
        )
    authority = scoped_authority(devices=("home.light",)).model_copy(
        update={"capabilities": ["brightness"]}
    )
    arguments = {"devices": ["home.light"], "allow_stale": False}
    if selection is not None:
        arguments["capabilities"] = selection
    with request_authority(authority):
        payload = structured(await server.call_tool("get_state", arguments))
    if selection == ["temperature"]:
        assert payload["error"]["code"] == "insufficient_scope"
    else:
        assert [state["capability"] for state in payload["states"]] == ["brightness"]
    assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unauthenticated", "foreign_scope"])
async def test_status_recovery_checks_authority_before_any_repository_call(
    tmp_path, case, monkeypatch
):
    from tests.fixtures.household_onboarding import onboarding_gateway

    from domoai.application.household_onboarding import HouseholdOnboardingService
    from domoai.application.state_service import StateService
    from domoai.mcp.resources import coverage_evidence_snapshot

    async with onboarding_gateway(tmp_path) as gateway:
        runtime = gateway.runtime
        manager = runtime.connection_manager
        calls = []

        async def forbidden_query():
            calls.append("repository")
            raise AssertionError("unauthorized request reached repository")

        monkeypatch.setattr(manager.repository, "list_all", forbidden_query)
        monkeypatch.setattr(manager.repository, "has_unresolved_secret_transfers", forbidden_query)
        policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")
        service = HouseholdOnboardingService(
            registry=runtime.registry,
            state_service=StateService(runtime.state_store),
            manager=manager,
            authority_policy=policy,
            coverage_projector=coverage_evidence_snapshot,
            membership=lambda: (),
            readiness=lambda: [],
            public_url="http://localhost/mcp",
        )
        token = _management_token(token="fixture", scopes=["read"])
        token.claims["authority"]["tenant_id"] = "foreign"

        async def verify(_value):
            return token

        app = create_management_app(
            manager, auth_verifier=verify, authority_policy=policy, household_onboarding=service
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.get(
                "/api/v1/onboarding/status",
                headers=({"Authorization": "Bearer fixture"} if case == "foreign_scope" else {}),
            )
        assert response.status_code == (403 if case == "foreign_scope" else 401)
        assert calls == []


@pytest.mark.asyncio
async def test_mcp_allowed_but_undeclared_capability_never_becomes_unfiltered_read(monkeypatch):
    from tests.composition.test_scoped_area_inventory_composition import request_authority
    from tests.contract.test_effective_route_coverage import build_route_context
    from tests.contract.test_scoped_area_inventory_contract import scoped_authority
    from tests.contract.test_unified_mcp_contract import structured

    _, context, server = build_route_context((("a", True),), ("a",))
    calls = []

    async def forbidden_read(*_args, **_kwargs):
        calls.append("read")
        raise AssertionError("empty intersection reached unfiltered StateService")

    monkeypatch.setattr(context.state_service, "get_with_diagnostics", forbidden_read)
    authority = scoped_authority(devices=("home.light",)).model_copy(
        update={"capabilities": ["not_declared"]}
    )
    with request_authority(authority):
        payload = structured(await server.call_tool("get_state", {"devices": ["home.light"]}))
    assert payload["states"] == [] and payload["diagnostics"] == []
    assert calls == []
