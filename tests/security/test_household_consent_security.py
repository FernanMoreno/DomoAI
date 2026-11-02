"""T010 security RED; unavailable host paths are precursors, not human-consent proof."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp.server.auth.provider import AccessToken
from starlette.applications import Starlette
from starlette.types import Message, Scope
from tests.integration.test_management_api import StubManager

from domoai.application.authority import AuthorityPolicy
from domoai.config.settings import Settings
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import AuthorityContext, PrincipalRole
from domoai.management.api import create_management_app
from domoai.mcp.auth import ClientTokenDocument, ClientTokenRecord, StaticBearerTokenVerifier

_OPERATIONS = ("review_consent", "approve_consent", "revoke_consent")


def _authority(role: str = "owner", **changes: Any) -> AuthorityContext:
    values: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "household_id": "home-a",
        "household_ids": ["home-a"],
        "principal_id": "resident-a",
        "roles": [role],
    }
    values.update(changes)
    return AuthorityContext.model_validate(values)


def _policy() -> AuthorityPolicy:
    return AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("role", ["owner", "operator"])
@pytest.mark.parametrize("case", ["omitted", "empty", "foreign", "explicit"])
def test_t010_a_resident_operation_requires_explicit_permission(
    operation: str, role: str, case: str
) -> None:
    # Independent 3 x 2 x 4 oracle. Empty must not become permissive in T011.
    options: dict[str, Any] = {}
    if case == "empty":
        options["operations"] = []
    elif case == "foreign":
        options["operations"] = ["inspect"]
    elif case == "explicit":
        options["operations"] = [operation]
    authority = _authority(role, **options)
    assert ("operations" in authority.model_fields_set) is (case != "omitted")
    if case == "explicit":
        _policy().authorize(authority, operation=operation)
    else:
        with pytest.raises(DomainError) as rejected:
            _policy().authorize(authority, operation=operation)
        assert rejected.value.code is ErrorCode.INSUFFICIENT_SCOPE


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("role", ["viewer", "planner", "service"])
def test_t010_a_non_resident_role_cannot_gain_human_operation(operation: str, role: str) -> None:
    with pytest.raises(DomainError) as rejected:
        _policy().authorize(_authority(role, operations=[operation]), operation=operation)
    assert rejected.value.code is ErrorCode.INSUFFICIENT_SCOPE


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize(
    "scope", ["tenant", "membership", "runtime", "area", "device", "capability"]
)
def test_t010_a_every_resident_target_stays_inside_verified_scope(
    operation: str, scope: str
) -> None:
    # Owner with an explicit operation reaches today's ACL checks, not role denial.
    authority = _authority(operations=[operation])
    _policy().authorize(authority, operation=operation)
    arguments: dict[str, Any] = {"operation": operation}
    if scope == "tenant":
        authority = _authority(tenant_id="tenant-b", operations=[operation])
    elif scope == "membership":
        authority = _authority(
            household_id="home-b", household_ids=["home-b"], operations=[operation]
        )
    elif scope == "runtime":
        authority = _authority(household_ids=["home-a", "home-b"], operations=[operation])
        arguments["target_household_id"] = "home-b"
    elif scope == "area":
        authority = _authority(area_ids=["living-room"], operations=[operation])
        arguments["area_id"] = "bedroom"
    elif scope == "device":
        authority = _authority(device_ids=["light-a"], operations=[operation])
        arguments["device_ids"] = ("light-a", "light-b")
    else:
        authority = _authority(capabilities=["turn_on"], operations=[operation])
        arguments["capabilities"] = ("turn_on", "unlock")
    with pytest.raises(DomainError) as rejected:
        _policy().authorize(authority, **arguments)
    assert rejected.value.code is ErrorCode.INSUFFICIENT_SCOPE


@pytest.mark.parametrize("role", [PrincipalRole.OWNER, PrincipalRole.SERVICE])
def test_t010_a_legacy_management_permission_keeps_existing_fallback(role: PrincipalRole) -> None:
    # No wildcard semantics are imposed by this resident-only slice.
    _policy().authorize(_authority(role), operation="manage_connections")


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("role", ["owner", "operator"])
def test_t010_a_resident_wildcard_is_not_explicit_permission(operation: str, role: str) -> None:
    with pytest.raises(DomainError) as rejected:
        _policy().authorize(_authority(role, operations=["*"]), operation=operation)
    assert rejected.value.code is ErrorCode.INSUFFICIENT_SCOPE


@pytest.mark.parametrize("operation", ["list", "execute", "manage_connections", "legacy_extension"])
@pytest.mark.parametrize("role", ["owner", "service"])
def test_t010_a_nonresident_operations_preserve_legacy_wildcard(operation: str, role: str) -> None:
    # Resident-only restrictions must not change existing read/operator/admin/fallback paths.
    _policy().authorize(_authority(role, operations=["*"]), operation=operation)


def _verifier(path: Path, **changes: Any) -> StaticBearerTokenVerifier:
    values: dict[str, Any] = {
        "client_id": "resident-a",
        "token_hash": hashlib.sha256(b"t010-resident-secret").hexdigest(),
        "tenant_id": "tenant-a",
        "household_ids": ["home-a"],
        "roles": ["operator"],
        "scopes": ["consent:human"],
        "operations": list(_OPERATIONS),
    }
    values.update(changes)
    document = ClientTokenDocument(clients=[ClientTokenRecord.model_validate(values)])
    path.write_text(document.model_dump_json(), encoding="utf-8")
    return StaticBearerTokenVerifier.from_file(path)


def _assert_unavailable(response: httpx.Response) -> None:
    assert response.status_code >= 400
    assert response.status_code not in {404, 405}, (
        "T010 precursor: resident endpoint is absent; no downstream security oracle reached"
    )
    payload = response.json()
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    assert code == "human_confirmation_unavailable"
    assert "t010-resident-secret" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("credential", ["loopback-anonymous", "management-static"])
@pytest.mark.parametrize("path", ["session", "reviews", "decisions"])
async def test_t010_b_unconfigured_resident_endpoint_is_explicitly_unavailable(
    credential: str, path: str
) -> None:
    manager = StubManager()
    secret = "t010-management-secret" if credential == "management-static" else None
    app = create_management_app(manager, auth_token=secret)
    headers = {"Origin": "http://localhost"}
    if secret is not None:
        headers["Authorization"] = f"Bearer {secret}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(f"/api/v1/resident/{path}", headers=headers, json={})
    assert manager.mutation_calls == 0
    _assert_unavailable(response)


async def test_t010_b_human_operations_do_not_authorize_connection_management(
    tmp_path: Path,
) -> None:
    # Hold owner and manage scope valid: only the operation ACL must reject.
    verifier = _verifier(tmp_path / "resident.json", roles=["owner"], scopes=["manage"])
    manager = StubManager()
    app = create_management_app(
        manager, auth_verifier=verifier.verify_token, authority_policy=_policy()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/v1/connections/setup",
            headers={"Authorization": "Bearer t010-resident-secret", "Origin": "http://localhost"},
            json={"action": "start", "connector_id": "fixture"},
        )
    assert response.status_code == 403
    assert manager.mutation_calls == 0
    assert "t010-resident-secret" not in response.text


@pytest.mark.parametrize(
    "scope,expected_status,expected_mutations",
    [("consent:human", 403, 0), ("manage", 201, 1)],
)
async def test_t010_b_scope_alone_controls_management_admission(
    tmp_path: Path, scope: str, expected_status: int, expected_mutations: int
) -> None:
    # Same explicit owner, operation, household, ACL and Origin; vary only scope.
    verifier = _verifier(
        tmp_path / "management.json",
        roles=["owner"],
        scopes=[scope],
        operations=["manage_connections"],
    )
    manager = StubManager()
    app = create_management_app(
        manager, auth_verifier=verifier.verify_token, authority_policy=_policy()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/v1/connections/setup",
            headers={"Authorization": "Bearer t010-resident-secret", "Origin": "http://localhost"},
            json={"action": "start", "connector_id": "fixture"},
        )
    assert response.status_code == expected_status
    assert manager.mutation_calls == expected_mutations
    assert "t010-resident-secret" not in response.text


@pytest.mark.parametrize("role", ["owner", "service"])
async def test_t010_b_explicit_management_permission_still_reaches_manager(
    tmp_path: Path, role: str
) -> None:
    verifier = _verifier(
        tmp_path / "management.json",
        roles=[role],
        scopes=["manage"],
        operations=["manage_connections"],
    )
    manager = StubManager()
    app = create_management_app(
        manager, auth_verifier=verifier.verify_token, authority_policy=_policy()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/v1/connections/setup",
            headers={"Authorization": "Bearer t010-resident-secret", "Origin": "http://localhost"},
            json={"action": "start", "connector_id": "fixture"},
        )
    assert response.status_code == 201
    assert manager.mutation_calls == 1


def _require_resident_routes(app: Starlette, expected: set[tuple[str, str]]) -> None:
    registered = {
        (str(getattr(route, "path", "")), method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or ())
    }
    assert expected <= registered, (
        "T010 precursor: real resident routes absent; downstream origin/session/challenge/"
        f"nonce/expiry behavior NOT reached: {sorted(expected - registered)}"
    )


def test_t010_b_resident_origin_guard_requires_its_own_post_routes_precursor() -> None:
    """Absent/null/cross-origin and forged Host are NOT tested by this route prerequisite.

    Once configured D2 exists, exercise those inputs through its real three POSTs;
    the legacy management _same_origin helper must not stand in for that guard.
    """
    _require_resident_routes(
        create_management_app(StubManager()),
        {
            ("/api/v1/resident/session", "POST"),
            ("/api/v1/resident/reviews", "POST"),
            ("/api/v1/resident/decisions", "POST"),
        },
    )


def test_t010_c_session_challenge_nonce_expiry_requires_real_host_precursor() -> None:
    """Only the absent HTTP contract is observable before the configured host exists.

    Not reached: server-created session/review, exact actor/hash/home/origin binding,
    missing/swapped session or approve/revoke challenge, replay/simultaneous nonce,
    session 600s/challenge 120s exact expiry, plan change, explicit gesture and receipt
    ownership. No synthetic principal/assertion/provider may turn this into proof.
    """
    _require_resident_routes(
        create_management_app(StubManager()),
        {
            ("/api/v1/resident/session", "POST"),
            ("/api/v1/resident/reviews", "POST"),
            ("/api/v1/resident/decisions", "POST"),
            ("/api/v1/resident/decisions/{request_id}", "GET"),
        },
    )


async def test_t010_d_shared_credentials_need_separate_configuration_precursor(
    tmp_path: Path,
) -> None:
    """Cross-store detection is un-reached; current verifier accepts a duplicated hash."""
    first = _verifier(tmp_path / "mcp.json")
    second = _verifier(tmp_path / "resident.json")
    assert await first.verify_token("t010-resident-secret") is not None
    assert await second.verify_token("t010-resident-secret") is not None
    assert "resident_token_file" in Settings.model_fields, (
        "T010 precursor: no resident credential configuration; shared file/hash rejection, "
        "configured origin and bootstrap separation NOT reached (T012)"
    )
    assert "resident_origin" in Settings.model_fields


async def test_t010_d_opaque_management_verifier_never_becomes_human_fallback(
    tmp_path: Path,
) -> None:
    verifier = _verifier(
        tmp_path / "management.json",
        roles=["owner"],
        scopes=["manage"],
        operations=["manage_connections"],
    )

    async def opaque(token: str) -> AccessToken | None:
        # Production verification behind the same opaque callback shape as gateway.
        return await verifier.verify_token(token)

    manager = StubManager()
    app = create_management_app(manager, auth_verifier=opaque, authority_policy=_policy())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/v1/resident/decisions",
            headers={"Authorization": "Bearer t010-resident-secret", "Origin": "http://localhost"},
            json={"approved_by": "resident-a", "confirmation": True, "operator_token": "legacy"},
        )
    assert manager.mutation_calls == 0
    _assert_unavailable(response)


@pytest.mark.parametrize("state", ["disabled", "revoked", "expired"])
async def test_t010_d_real_verifier_rejects_inactive_credential_control(
    tmp_path: Path, state: str
) -> None:
    changes_by_state: dict[str, dict[str, Any]] = {
        "disabled": {"enabled": False},
        "revoked": {"revoked_at": datetime.now(UTC) - timedelta(seconds=10)},
        "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=10)},
    }
    verifier = _verifier(tmp_path / "resident.json", **changes_by_state[state])
    assert await verifier.verify_token("t010-resident-secret") is None


async def test_t010_d_real_reload_removes_token_but_does_not_prove_session_invalidation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "resident.json"
    verifier = _verifier(path)
    assert await verifier.verify_token("t010-resident-secret") is not None
    path.write_text(json.dumps({"clients": []}), encoding="utf-8")
    verifier.reload()
    assert await verifier.verify_token("t010-resident-secret") is None


# T011-B only proves the unconfigured boundary, never human authorization.
_T011_PATHS = tuple(f"/api/v1/resident/{name}" for name in ("session", "reviews", "decisions"))


def _assert_t011_response(response: httpx.Response, status: int = 503) -> None:
    assert response.status_code == status
    assert response.json() == (
        {
            "error": "human_confirmation_unavailable",
            "message": "No está disponible la confirmación del residente",
        }
        if status == 503
        else {"error": "rate_limited"}
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    for name in ("set-cookie", "access-control-allow-origin", "location", "retry-after"):
        assert name not in response.headers


@pytest.mark.parametrize("path", _T011_PATHS)
async def test_t011_b_exact_post_unavailable(path: str) -> None:
    manager = StubManager()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_management_app(manager)),
        base_url="http://localhost",
    ) as client:
        response = await client.post(path)
    _assert_t011_response(response)
    assert manager.mutation_calls == 0
    assert not manager.audit.events


@pytest.mark.parametrize("path", _T011_PATHS)
@pytest.mark.parametrize("credential", [None, "invalid", "management-secret"])
@pytest.mark.parametrize("origin", [None, "null", "http://foreign", "http://localhost"])
async def test_t011_b_precedes_static_auth_and_origin(
    path: str, credential: str | None, origin: str | None
) -> None:
    manager = StubManager()
    headers = {}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    if origin is not None:
        headers["Origin"] = origin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_management_app(manager, auth_token="management-secret")
        ),
        base_url="http://localhost",
    ) as client:
        response = await client.post(path, headers=headers, json={})
    _assert_t011_response(response)
    assert manager.mutation_calls == 0
    assert not manager.audit.events


@pytest.mark.parametrize("path", _T011_PATHS)
@pytest.mark.parametrize("body", [b"", b"{", b"[]", b"null", b"x" * 65])
async def test_t011_b_precedes_body_parser(path: str, body: bytes) -> None:
    manager = StubManager()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_management_app(manager, max_request_body_size=64)),
        base_url="http://localhost",
    ) as client:
        response = await client.post(path, content=body, headers={"Content-Type": "text/plain"})
    _assert_t011_response(response)
    assert manager.mutation_calls == 0
    assert not manager.audit.events


@pytest.mark.parametrize("path", _T011_PATHS)
async def test_t011_b_custom_verifier_and_policy_are_not_called(tmp_path: Path, path: str) -> None:
    verifier = _verifier(
        tmp_path / "management.json",
        roles=["owner"],
        scopes=["manage"],
        operations=["manage_connections"],
    )
    calls: list[str] = []

    async def observed_verifier(token: str) -> AccessToken | None:
        calls.append("verifier")
        return await verifier.verify_token(token)

    class ObservedPolicy(AuthorityPolicy):
        def authorize(self, authority: AuthorityContext, **request: Any) -> None:
            calls.append("policy")
            super().authorize(authority, **request)

    manager = StubManager()
    app = create_management_app(
        manager,
        auth_verifier=observed_verifier,
        authority_policy=ObservedPolicy(tenant_id="tenant-a", household_id="home-a"),
        rate_limit=1,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        first = await client.post(path, headers={"Authorization": "Bearer t010-resident-secret"})
        # All three exact routes share one budget, independent of credentials/Origin.
        limited = [
            await client.post(other, headers={"Authorization": "Bearer invalid", "Origin": "null"})
            for other in _T011_PATHS
        ]
        public = await client.get("/")
    assert calls == []
    assert not manager.audit.events
    assert manager.mutation_calls == 0
    _assert_t011_response(first)
    for response in limited:
        _assert_t011_response(response, 429)
    assert public.status_code == 200  # Different namespace, not a global budget.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.2", 123)),
        base_url="http://localhost",
    ) as other_client:
        _assert_t011_response(await other_client.post(path))


@pytest.mark.parametrize("path", _T011_PATHS)
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_t011_b_other_methods_retain_management_guard(
    path: str, method: str, authorized: bool
) -> None:
    app = create_management_app(StubManager(), auth_token="management-secret")
    headers = {"Authorization": "Bearer management-secret"} if authorized else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.request(method, path, headers=headers)
    assert response.status_code == (405 if authorized else 401)


@pytest.mark.parametrize("path", _T011_PATHS)
@pytest.mark.parametrize("status", [503, 429])
async def test_t011_b_no_manager_attribute_access(path: str, status: int) -> None:
    target = StubManager()
    accesses: list[str] = []

    class ObservedManager:
        def __getattribute__(self, name: str) -> Any:
            # Observe every boundary access, including list/get/audit/property reads.
            # Delegation preserves the real stub behavior; no synthetic DB/provider.
            accesses.append(name)
            return getattr(target, name)

    app = create_management_app(ObservedManager(), rate_limit=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        if status == 429:
            _assert_t011_response(await client.post(path))
        accesses.clear()  # Only observe the request under test, not construction/priming.
        response = await client.post(path)
    assert accesses == [], f"resident rejection accessed manager: {accesses}"
    _assert_t011_response(response, status)
    assert target.mutation_calls == 0
    assert not target.audit.events


async def _t011_response_without_body_completion(app: Starlette, path: str) -> httpx.Response:
    """Observe the complete ASGI response while the request's final chunk is withheld.

    Infrastructure may poll receive/drain a prefix. This does not assert zero receive
    or physical TCP behavior; the application decision must not depend on the tail.
    """
    release_tail = anyio.Event()
    response_complete = anyio.Event()
    messages: list[Message] = []
    prefix_sent = False

    async def receive() -> Message:
        nonlocal prefix_sent
        if not prefix_sent:
            prefix_sent = True
            return {"type": "http.request", "body": b'{"data":"' + b"x" * 4096, "more_body": True}
        await release_tail.wait()
        return {"type": "http.request", "body": b'"}', "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            response_complete.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 123),
        "server": ("localhost", 80),
    }
    async with anyio.create_task_group() as group:
        group.start_soon(app, scope, receive, send)
        try:
            with anyio.move_on_after(2) as deadline:
                await response_complete.wait()
        finally:
            # Cancellation and release are cleanup, never the success condition.
            group.cancel_scope.cancel()
            release_tail.set()
    assert not deadline.cancel_called, "application response waited for withheld request body"
    start = next(message for message in messages if message["type"] == "http.response.start")
    return httpx.Response(
        start["status"],
        headers=start["headers"],
        content=b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        ),
    )


@pytest.mark.parametrize("path", _T011_PATHS)
@pytest.mark.parametrize("status", [503, 429])
async def test_t011_b_response_does_not_wait_for_body(path: str, status: int) -> None:
    app = create_management_app(StubManager(), rate_limit=1, max_request_body_size=64)
    if status == 429:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            _assert_t011_response(await client.post(path))
    _assert_t011_response(await _t011_response_without_body_completion(app, path), status)


async def test_t011_b_legacy_guard_can_reject_without_body_completion_control() -> None:
    app = create_management_app(StubManager(), auth_token="management-secret")
    response = await _t011_response_without_body_completion(app, "/api/v1/connections/setup")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/resident/session/",
        "/api/v1/resident/session-extra",
        "/api/v1/resident/unknown",
        "/api/v1/resident/decisions/request-id",
        "/api/v1/connections/setup",
    ],
)
async def test_t011_b_neighbor_paths_do_not_bypass_management(path: str) -> None:
    app = create_management_app(StubManager(), auth_token="management-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(path, headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401


async def test_t010_d_malformed_reload_retains_legacy_verifier_not_human_availability(
    tmp_path: Path,
) -> None:
    """Existing MCP contract only; D2 must wrap failure with session invalidation later."""
    path = tmp_path / "resident.json"
    verifier = _verifier(path)
    assert await verifier.verify_token("t010-resident-secret") is not None
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid MCP client token file"):
        verifier.reload()
    assert await verifier.verify_token("t010-resident-secret") is not None


@pytest.fixture
def c1_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real provisioned files; missing admission API is a call-time RED precursor."""
    import importlib

    from domoai.application.connection_manager import ConnectionManager
    from domoai.mcp.token_lifecycle import TokenFileManager
    from domoai.persistence.repositories import PlanRepository
    from domoai.runtime.approval_store import ApprovalStore

    paths = {name: tmp_path / f"{name}.json" for name in ("resident", "mcp", "management")}
    tokens = {}
    verifiers = {}
    for name, path in paths.items():
        tokens[name] = (
            TokenFileManager(path)
            .rotate(
                name,
                scopes=["consent:human"] if name == "resident" else ["manage"],
                tenant_id="tenant-a",
                household_ids=["home-a"],
                roles=[PrincipalRole.OWNER],
                operations=["review_consent"] if name == "resident" else ["manage_connections"],
                area_ids=["room-a"],
                device_ids=["light-a"],
                capabilities=["switch"],
            )
            .token
        )
        verifiers[name] = StaticBearerTokenVerifier.from_file(path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("C1 crossed manager/target/store/assertion boundary")

    for owner, names in (
        (ConnectionManager, ("get", "list", "create")),
        (PlanRepository, ("get", "save")),
        (ApprovalStore, ("__init__", "issue", "issue_assertion", "prepare_delivery")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    # Import is inside a helper called by the test, not fixture setup/collection.
    return {
        "load_module": lambda: importlib.import_module("domoai.management.consent"),
        "paths": paths,
        "tokens": tokens,
        "verifiers": verifiers,
        "settings": Settings(
            resident_token_file=paths["resident"], resident_origin="https://home.example"
        ),
    }


def _c1_admission(files: dict[str, Any], **changes: Any) -> Any:
    module = files["load_module"]()
    peers = module.ResidentPeerInventory(
        frozenset({"mcp", "management", "management-static"}),
        (
            module.InspectableCredentialPeer("mcp", files["verifiers"]["mcp"]),
            module.InspectableCredentialPeer("management", files["verifiers"]["management"]),
            module.AbsentCredentialPeer("management-static"),
        ),
    )
    options = {
        "settings": files["settings"],
        "resident_verifier": files["verifiers"]["resident"],
        "peers": peers,
        "authority_policy": _policy(),
    }
    options.update(changes)
    return module.ResidentAdmission(**options)


async def _c1_authenticate(admission: Any, token: str, **headers: Any) -> Any:
    return await admission.authenticate(
        headers.get("authorization", f"Bearer {token}"),
        origin=headers.get("origin", "https://home.example"),
        host=headers.get("host", "home.example"),
    )


def _c1_error(error: Any, status: int) -> None:
    expected = {
        401: (ErrorCode.OPERATOR_AUTHENTICATION_FAILED, "La credencial del residente no es válida"),
        403: (ErrorCode.INSUFFICIENT_SCOPE, "La solicitud del residente no está autorizada"),
        503: (
            ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE,
            "No está disponible la confirmación del residente",
        ),
    }
    assert error.status_code == status
    assert (error.code, error.message) == expected[status]
    assert error.details == {} and error.retryable is False
    assert error.field is None and error.device_id is None and error.capability is None


def _c1_change_record(files: dict[str, Any], **changes: Any) -> None:
    path = files["paths"]["resident"]
    document = json.loads(path.read_text())
    document["clients"][0].update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")
    files["verifiers"]["resident"].reload()


@pytest.mark.parametrize("role", ["owner", "operator"])
async def test_t011_c_admission_identity_only(c1_files: dict[str, Any], role: str) -> None:
    from dataclasses import FrozenInstanceError, fields

    _c1_change_record(c1_files, roles=[role])
    admission = _c1_admission(c1_files)
    token = c1_files["tokens"]["resident"]
    identity = await _c1_authenticate(admission, token)
    assert identity.principal_id == "resident" and identity.tenant_id == "tenant-a"
    assert identity.household_id == "home-a" and identity.household_ids == ("home-a",)
    assert identity.roles == (PrincipalRole(role),) and identity.scopes == ("consent:human",)
    assert identity.area_ids == ("room-a",) and identity.device_ids == ("light-a",)
    assert identity.capabilities == ("switch",) and identity.operations == ("review_consent",)
    assert identity.credential_fingerprint == hashlib.sha256(token.encode()).hexdigest()
    assert (
        identity.credential_revision
        == c1_files["verifiers"]["resident"].credential_snapshot().revision_digest
    )
    assert type(identity.credential_expires_at) is datetime
    assert identity.origin == "https://home.example"
    assert {field.name for field in fields(identity)} == {
        "principal_id",
        "tenant_id",
        "household_id",
        "household_ids",
        "roles",
        "scopes",
        "area_ids",
        "device_ids",
        "capabilities",
        "operations",
        "credential_fingerprint",
        "credential_revision",
        "credential_expires_at",
        "origin",
    }
    with pytest.raises(FrozenInstanceError):
        identity.principal_id = "other"
    assert token not in repr(identity) and identity.credential_fingerprint not in repr(identity)
    # Constructing a valid component does not configure the management HTTP app.
    manager = StubManager()
    app = create_management_app(manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        _assert_t011_response(
            await client.post(
                "/api/v1/resident/session", headers={"Authorization": f"Bearer {token}"}
            )
        )
    assert manager.mutation_calls == 0


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic good",
        "Bearer",
        "Bearer ",
        "Bearer  bad",
        "Bearer\tbad",
        " Bearer bad",
        "Bearer bad ",
        "Bearer bad,good",
        "Bearer bä d",
        "Bearer invalid",
    ],
)
async def test_t011_c_admission_invalid_bearer_stable(
    c1_files: dict[str, Any], header: str | None
) -> None:
    admission = _c1_admission(c1_files)
    module = c1_files["load_module"]()
    with pytest.raises(module.ResidentAdmissionError) as caught:
        await _c1_authenticate(admission, "", authorization=header)
    _c1_error(caught.value, 401)
    assert (
        await _c1_authenticate(admission, c1_files["tokens"]["resident"])
    ).principal_id == "resident"


@pytest.mark.parametrize("state", ["disabled", "revoked", "expired"])
async def test_t011_c_admission_inactive_bearer(c1_files: dict[str, Any], state: str) -> None:
    changes: dict[str, dict[str, object]] = {
        "disabled": {"enabled": False},
        "revoked": {"revoked_at": datetime.now(UTC).isoformat()},
        "expired": {"created_at": "2019-01-01T00:00:00Z", "expires_at": "2020-01-01T00:00:00Z"},
    }
    _c1_change_record(c1_files, **changes[state])
    admission = _c1_admission(c1_files)
    with pytest.raises(c1_files["load_module"]().ResidentAdmissionError) as caught:
        await _c1_authenticate(admission, c1_files["tokens"]["resident"])
    _c1_error(caught.value, 401)


@pytest.mark.parametrize(
    "origin,host",
    [
        (None, "home.example"),
        ("null", "home.example"),
        ("https://evil.example", "evil.example"),
        ("http://home.example", "home.example"),
        ("https://home.example:8443", "home.example"),
        ("https://home.example/", "home.example"),
        ("https://HOME.example", "home.example"),
        (" https://home.example", "home.example"),
        ("https://home.example", None),
        ("https://home.example", "home.example:443"),
        ("https://home.example", "HOME.example"),
        ("https://home.example", "evil.example"),
    ],
)
async def test_t011_c_admission_trusted_origin_precedes_bearer(
    c1_files: dict[str, Any], monkeypatch: pytest.MonkeyPatch, origin: str | None, host: str | None
) -> None:
    admission = _c1_admission(c1_files)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid origin reached verifier")

    monkeypatch.setattr(StaticBearerTokenVerifier, "verify_token", forbidden)
    with pytest.raises(c1_files["load_module"]().ResidentAdmissionError) as caught:
        await _c1_authenticate(admission, "invalid", origin=origin, host=host)
    _c1_error(caught.value, 403)


@pytest.mark.parametrize(
    "case",
    [
        "resident-none",
        "inventory-none",
        "missing",
        "duplicate",
        "empty-manifest",
        "extra-unknown",
        "mcp-unknown",
        "management-unknown",
        "static-unknown",
        "resident-source-unknown",
        "peer-source-unknown",
        "settings-none",
        "settings-noncanonical",
        "settings-http-remote",
    ],
)
async def test_t011_c_admission_unknown_configuration(
    c1_files: dict[str, Any], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    module = c1_files["load_module"]()
    changes: dict[str, Any] = {}
    required = frozenset({"mcp", "management", "management-static"})
    peers = [
        module.InspectableCredentialPeer("mcp", c1_files["verifiers"]["mcp"]),
        module.InspectableCredentialPeer("management", c1_files["verifiers"]["management"]),
        module.AbsentCredentialPeer("management-static"),
    ]
    if case == "resident-none":
        changes["resident_verifier"] = None
    elif case == "inventory-none":
        changes["peers"] = None
    elif case == "missing":
        peers.pop()
    elif case == "duplicate":
        peers.append(peers[0])
    elif case == "empty-manifest":
        required = frozenset()
    elif case == "extra-unknown":
        required |= {"custom"}
        peers.append(module.UnknownCredentialPeer("custom"))
    elif case.endswith("-unknown") and case.split("-")[0] in {"mcp", "management", "static"}:
        index = {"mcp": 0, "management": 1, "static": 2}[case.split("-")[0]]
        peers[index] = module.UnknownCredentialPeer(peers[index].peer_id)
    elif case in {"resident-source-unknown", "peer-source-unknown"}:
        name = "resident" if case.startswith("resident") else "mcp"
        document = ClientTokenDocument.model_validate_json(c1_files["paths"][name].read_text())
        direct = StaticBearerTokenVerifier(tuple(document.clients))
        if name == "resident":
            changes["resident_verifier"] = direct
        else:
            peers[0] = module.InspectableCredentialPeer("mcp", direct)
    else:
        origin = {
            "settings-none": None,
            "settings-noncanonical": "https://HOME.example",
            "settings-http-remote": "http://home.example",
        }[case]
        changes["settings"] = c1_files["settings"].model_copy(update={"resident_origin": origin})
    if "peers" not in changes:
        changes["peers"] = module.ResidentPeerInventory(required, tuple(peers))
    admission = _c1_admission(c1_files, **changes)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unavailable reached bearer/policy")

    monkeypatch.setattr(StaticBearerTokenVerifier, "verify_token", forbidden)
    monkeypatch.setattr(AuthorityPolicy, "authorize", forbidden)
    with pytest.raises(module.ResidentAdmissionError) as caught:
        await _c1_authenticate(admission, "invalid", origin="https://evil", host="evil")
    _c1_error(caught.value, 503)


@pytest.mark.parametrize(
    "case",
    [
        "marker",
        "scope-manage",
        "scope-wildcard",
        "role-service",
        "role-viewer",
        "role-planner",
        "operation-empty",
        "operation-wildcard",
        "operation-foreign",
        "omit-operations",
        "omit-tenant",
        "omit-households",
        "omit-roles",
        "tenant",
        "membership",
        "runtime",
    ],
)
async def test_t011_c_admission_explicit_claims_and_policy(
    c1_files: dict[str, Any], case: str
) -> None:
    path = c1_files["paths"]["resident"]
    document = json.loads(path.read_text())
    record = document["clients"][0]
    policy = _policy()
    if case == "marker":
        record["management_authority_explicit"] = False
    elif case.startswith("scope-"):
        record["scopes"] = ["*" if case == "scope-wildcard" else "manage"]
    elif case.startswith("role-"):
        record["roles"] = [case.removeprefix("role-")]
    elif case.startswith("operation-"):
        record["operations"] = {
            "operation-empty": [],
            "operation-wildcard": ["*"],
            "operation-foreign": ["manage_connections"],
        }[case]
    elif case.startswith("omit-"):
        field = {
            "omit-operations": "operations",
            "omit-tenant": "tenant_id",
            "omit-households": "household_ids",
            "omit-roles": "roles",
        }[case]
        del record[field]
        if case == "omit-tenant":
            policy = AuthorityPolicy(tenant_id="default", household_id="home-a")
        if case == "omit-households":
            policy = AuthorityPolicy(tenant_id="tenant-a", household_id="default")
    elif case == "tenant":
        record["tenant_id"] = "tenant-b"
    elif case == "membership":
        record["household_ids"] = ["home-b"]
    else:
        policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-b")
    path.write_text(json.dumps(document), encoding="utf-8")
    c1_files["verifiers"]["resident"].reload()
    admission = _c1_admission(c1_files, authority_policy=policy)
    assert (
        await c1_files["verifiers"]["resident"].verify_token(c1_files["tokens"]["resident"])
        is not None
    )
    with pytest.raises(c1_files["load_module"]().ResidentAdmissionError) as caught:
        await _c1_authenticate(admission, c1_files["tokens"]["resident"])
    _c1_error(caught.value, 403)


@pytest.mark.parametrize(
    "case",
    [
        "same-instance",
        "symlink",
        "same-canonical-history",
        "hardlink-history",
        "copied-hash",
        "disabled-shared",
        "revoked-shared",
        "expired-shared",
        "management-shared",
    ],
)
async def test_t011_c_admission_separation_all_sources_and_hashes(
    c1_files: dict[str, Any], case: str
) -> None:
    from domoai.mcp.token_lifecycle import TokenFileManager

    resident = c1_files["verifiers"]["resident"]
    path = c1_files["paths"]["resident"]
    if case == "same-instance":
        c1_files["verifiers"]["mcp"] = resident
    elif case == "symlink":
        alias = path.with_name("alias.json")
        alias.symlink_to(path)
        c1_files["verifiers"]["mcp"] = StaticBearerTokenVerifier.from_file(alias)
    elif case == "same-canonical-history":
        TokenFileManager(path).rotate("resident", scopes=["manage"])
        c1_files["verifiers"]["mcp"] = StaticBearerTokenVerifier.from_file(path)
        assert (
            resident.credential_snapshot().records[0].token_hash
            != c1_files["verifiers"]["mcp"].credential_snapshot().records[0].token_hash
        )
    elif case == "hardlink-history":
        alias = path.with_name("hardlink.json")
        alias.hardlink_to(path)
        alias.write_text(c1_files["paths"]["mcp"].read_text(), encoding="utf-8")
        c1_files["verifiers"]["mcp"] = StaticBearerTokenVerifier.from_file(alias)
        assert (
            resident.credential_snapshot().records[0].token_hash
            != c1_files["verifiers"]["mcp"].credential_snapshot().records[0].token_hash
        )
    else:
        peer_name = "management" if case == "management-shared" else "mcp"
        peer = json.loads(c1_files["paths"][peer_name].read_text())["clients"][0]
        document = json.loads(path.read_text())
        shadow = dict(peer, client_id="shadow")
        if case == "disabled-shared":
            shadow["enabled"] = False
        if case == "revoked-shared":
            shadow["revoked_at"] = datetime.now(UTC).isoformat()
        if case == "expired-shared":
            shadow.update(created_at="2019-01-01T00:00:00Z", expires_at="2020-01-01T00:00:00Z")
        document["clients"].append(shadow)
        path.write_text(json.dumps(document), encoding="utf-8")
        resident.reload()
    admission = _c1_admission(c1_files)
    with pytest.raises(c1_files["load_module"]().ResidentAdmissionError) as caught:
        await _c1_authenticate(admission, c1_files["tokens"]["resident"])
    _c1_error(caught.value, 503)


@pytest.mark.parametrize(
    "transition",
    [
        "stable-valid",
        "stable-none",
        "peer-reload",
        "resident-reload",
        "close-valid",
        "close-none",
        "close-failed-reload",
    ],
)
async def test_t011_c_admission_postawait(
    c1_files: dict[str, Any], monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    from domoai.mcp.token_lifecycle import TokenFileManager

    admission = _c1_admission(c1_files)
    module = c1_files["load_module"]()
    resident = c1_files["verifiers"]["resident"]
    before = {key: value.credential_snapshot() for key, value in c1_files["verifiers"].items()}
    saved_path = c1_files["paths"]["resident" if transition == "resident-reload" else "mcp"]
    backup = saved_path.with_suffix(".saved")
    backup.hardlink_to(saved_path)
    entered, release = anyio.Event(), anyio.Event()
    original = StaticBearerTokenVerifier.verify_token
    calls: list[object] = []
    results: list[Any] = []
    raw_results: list[Any] = []
    policy_calls: list[object] = []
    identity_calls: list[object] = []
    original_policy = AuthorityPolicy.authorize
    original_identity = module.ResidentAdmissionIdentity

    def observe_policy(*args: Any, **kwargs: Any) -> Any:
        policy_calls.append(args)
        return original_policy(*args, **kwargs)

    def observe_identity(*args: Any, **kwargs: Any) -> Any:
        identity_calls.append(kwargs)
        return original_identity(*args, **kwargs)

    async def paused(self: StaticBearerTokenVerifier, token: str) -> AccessToken | None:
        assert self is resident, "peer verifier was used as fallback"
        calls.append(self)
        entered.set()
        with anyio.fail_after(5):
            await release.wait()
        result = await original(self, token)
        raw_results.append(result)
        return result

    monkeypatch.setattr(StaticBearerTokenVerifier, "verify_token", paused)
    monkeypatch.setattr(AuthorityPolicy, "authorize", observe_policy)
    monkeypatch.setattr(module, "ResidentAdmissionIdentity", observe_identity)
    token = (
        "incorrect"
        if transition in {"stable-none", "close-none"}
        else c1_files["tokens"]["resident"]
    )

    async def run() -> None:
        try:
            results.append(await _c1_authenticate(admission, token))
        except module.ResidentAdmissionError as error:
            results.append(error)

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        try:
            with anyio.fail_after(5):
                await entered.wait()
            if transition.endswith("reload") and not transition.startswith("close"):
                name = "resident" if transition == "resident-reload" else "mcp"
                TokenFileManager(saved_path).rotate(name, scopes=["manage"])
                c1_files["verifiers"][name].reload()
            elif transition.startswith("close"):
                admission.close()
                admission.close()
                assert {
                    key: value.credential_snapshot() for key, value in c1_files["verifiers"].items()
                } == before
                if transition == "close-failed-reload":
                    path = c1_files["paths"]["resident"]
                    path.write_text("{", encoding="utf-8")
                    with pytest.raises(ValueError):
                        resident.reload()
        finally:
            release.set()
    assert len(results) == len(raw_results) == 1
    assert (raw_results[0] is None) is (
        transition in {"stable-none", "close-none", "resident-reload"}
    )
    if transition == "stable-valid":
        assert results[0].principal_id == "resident"
        return
    if transition == "stable-none":
        _c1_error(results[0], 401)
        return
    _c1_error(results[0], 503)
    assert policy_calls == identity_calls == []
    if transition in {"resident-reload", "peer-reload"}:
        backup.replace(saved_path)
        c1_files["verifiers"]["resident" if transition == "resident-reload" else "mcp"].reload()
    assert {
        key: value.credential_snapshot() for key, value in c1_files["verifiers"].items()
    } == before
    with pytest.raises(module.ResidentAdmissionError) as caught:
        await _c1_authenticate(admission, c1_files["tokens"]["resident"])
    _c1_error(caught.value, 503)
    assert len(calls) == 1 and policy_calls == identity_calls == []


async def test_t011_c_admission_absent_and_empty_are_distinct_known_states(
    c1_files: dict[str, Any],
) -> None:
    path = c1_files["paths"]["mcp"]
    path.write_text('{"clients": []}', encoding="utf-8")
    c1_files["verifiers"]["mcp"].reload()
    admission = _c1_admission(c1_files)
    assert (
        await _c1_authenticate(admission, c1_files["tokens"]["resident"])
    ).principal_id == "resident"


@pytest.mark.parametrize(
    "origin,host",
    [
        ("https://home.example", "home.example"),
        ("https://home.example:8443", "home.example:8443"),
        ("http://[::1]:8000", "[::1]:8000"),
    ],
)
async def test_t011_c_admission_canonical_config_is_captured_without_disk_reread(
    c1_files: dict[str, Any], origin: str, host: str
) -> None:
    settings = Settings(resident_token_file=c1_files["paths"]["resident"], resident_origin=origin)
    admission = _c1_admission(c1_files, settings=settings)
    settings.resident_origin = "https://attacker.example"
    for path in c1_files["paths"].values():
        path.unlink()
    identity = await _c1_authenticate(
        admission, c1_files["tokens"]["resident"], origin=origin, host=host
    )
    assert identity.origin == origin and identity.principal_id == "resident"


@pytest.mark.parametrize("state", ["disabled", "revoked", "expired"])
async def test_t011_c_admission_inactive_peer_hash_still_excludes_resident(
    c1_files: dict[str, Any], state: str
) -> None:
    """Filtering inactive peer records must not admit their shared resident hash."""
    resident = c1_files["verifiers"]["resident"]
    peer = c1_files["verifiers"]["mcp"]
    token = c1_files["tokens"]["resident"]
    shadow = dict(
        json.loads(c1_files["paths"]["resident"].read_text())["clients"][0],
        client_id="inactive-peer-shadow",
    )
    if state == "disabled":
        shadow["enabled"] = False
    elif state == "revoked":
        shadow["revoked_at"] = datetime.now(UTC).isoformat()
    else:
        shadow.update(created_at="2019-01-01T00:00:00Z", expires_at="2020-01-01T00:00:00Z")
    path = c1_files["paths"]["mcp"]
    document = json.loads(path.read_text())
    document["clients"].append(shadow)
    path.write_text(json.dumps(document), encoding="utf-8")
    peer.reload()

    # Real verifier controls isolate inactivity on the peer side, not bad bearer.
    assert await resident.verify_token(token) is not None
    assert await peer.verify_token(token) is None
    assert await peer.verify_token(c1_files["tokens"]["mcp"]) is not None
    resident_snapshot, peer_snapshot = resident.credential_snapshot(), peer.credential_snapshot()
    resident_source, peer_source = resident_snapshot.source, peer_snapshot.source
    assert resident_source is not None and peer_source is not None
    assert resident_source.canonical_path != peer_source.canonical_path
    assert resident_source.device_id > 0 and resident_source.inode > 0
    assert peer_source.device_id > 0 and peer_source.inode > 0
    assert (resident_source.device_id, resident_source.inode) != (
        peer_source.device_id,
        peer_source.inode,
    )
    assert resident_snapshot.records[0].token_hash == peer_snapshot.records[-1].token_hash

    admission = _c1_admission(c1_files)
    with pytest.raises(c1_files["load_module"]().ResidentAdmissionError) as caught:
        await _c1_authenticate(admission, token)
    _c1_error(caught.value, 503)


@pytest.fixture
def c2_store(c1_files: dict[str, Any]) -> dict[str, Any]:
    from domoai.mcp.token_lifecycle import TokenFileManager
    from domoai.runtime.clock import FixedClock

    other = TokenFileManager(c1_files["paths"]["resident"]).rotate(
        "resident-other",
        scopes=["consent:human"],
        tenant_id="tenant-a",
        household_ids=["home-a"],
        roles=[PrincipalRole.OPERATOR],
        operations=["review_consent"],
    )
    c1_files["tokens"]["other"] = other.token
    c1_files["verifiers"]["resident"].reload()
    wall, tick = FixedClock(datetime.now(UTC)), [1000.0]
    admission = _c1_admission(c1_files)
    return dict(c1_files, admission=admission, wall=wall, tick=tick)


async def _c2_create(files: dict[str, Any], actor: str = "resident") -> Any:
    if "adapter" not in files:
        files["adapter"] = files["load_module"]().ResidentConsentAdapter(
            admission=files["admission"],
            clock=files["wall"],
            monotonic=lambda: files["tick"][0],
        )
    return await files["adapter"].create_session(
        "Bearer " + files["tokens"][actor], origin="https://home.example", host="home.example"
    )


async def _c2_validate(files: dict[str, Any], session_id: Any, actor: str = "resident") -> Any:
    return await files["adapter"].authenticate_session(
        session_id,
        "Bearer " + files["tokens"][actor],
        origin="https://home.example",
        host="home.example",
    )


async def test_t011_c_session_create_and_binding(
    c2_store: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re
    import secrets
    from dataclasses import FrozenInstanceError

    original, sizes = secrets.token_urlsafe, []

    def observed(nbytes: int) -> str:
        sizes.append(nbytes)
        return original(nbytes)

    monkeypatch.setattr(secrets, "token_urlsafe", observed)
    first = await _c2_create(c2_store)
    second = await _c2_create(c2_store)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first.session_id)
    assert first.session_id != second.session_id and sizes == [32, 32]
    assert first.issued_at == c2_store["wall"].now()
    assert first.expires_at == first.issued_at + timedelta(seconds=600)
    assert first.identity.principal_id == "resident"
    assert first.identity.tenant_id == "tenant-a" and first.identity.household_id == "home-a"
    assert first.identity.origin == "https://home.example"
    assert first.identity.device_ids == ("light-a",)
    assert (
        first.identity.credential_fingerprint
        == hashlib.sha256(c2_store["tokens"]["resident"].encode()).hexdigest()
    )
    assert (
        first.identity.credential_revision
        == c2_store["verifiers"]["resident"].credential_snapshot().revision_digest
    )
    assert type(first.issued_at) is type(first.expires_at) is datetime
    assert c2_store["tokens"]["resident"] not in repr(first)
    assert first.session_id not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.session_id = "changed"
    c2_store["wall"].set(first.issued_at + timedelta(seconds=599))
    c2_store["tick"][0] += 599
    assert await _c2_validate(c2_store, first.session_id) == first
    assert c2_store["adapter"].session_count == 2
    assert sizes == [32, 32], "validation must not mint or refresh sessions"
    module = c2_store["load_module"]()
    for session_id, actor in (
        (first.session_id, "other"),
        (None, "resident"),
        ("unknown", "resident"),
    ):
        with pytest.raises(module.ResidentSessionError) as caught:
            await _c2_validate(c2_store, session_id, actor)
        assert caught.value.status_code == 401
        assert caught.value.code == ErrorCode.OPERATOR_AUTHENTICATION_FAILED
        assert caught.value.message == "La sesión del residente no es válida"


@pytest.mark.parametrize("boundary", [599, 600, 601])
@pytest.mark.parametrize("clock_kind", ["utc", "monotonic"])
async def test_t011_c_session_expiry_non_sliding(
    c2_store: dict[str, Any],
    clock_kind: str,
    boundary: int,
) -> None:
    session = await _c2_create(c2_store)
    if clock_kind == "utc":
        c2_store["wall"].set(session.issued_at + timedelta(seconds=boundary))
    else:
        c2_store["tick"][0] += boundary
    if boundary < 600:
        assert await _c2_validate(c2_store, session.session_id) == session
    else:
        with pytest.raises(c2_store["load_module"]().ResidentSessionError) as caught:
            await _c2_validate(c2_store, session.session_id)
        assert caught.value.status_code == 401
        assert c2_store["adapter"].session_count == 0


async def test_t011_c_session_credential_deadline(c1_files: dict[str, Any]) -> None:
    from domoai.runtime.clock import FixedClock

    expiry = datetime.now(UTC) + timedelta(seconds=90)
    _c1_change_record(c1_files, expires_at=expiry.isoformat())
    module = c1_files["load_module"]()
    wall = FixedClock(datetime.now(UTC))
    adapter = module.ResidentConsentAdapter(admission=_c1_admission(c1_files), clock=wall)
    files = dict(c1_files, adapter=adapter)
    session = await _c2_create(files)
    assert session.expires_at == expiry
    wall.set(expiry)
    with pytest.raises(module.ResidentSessionError):
        await _c2_validate(files, session.session_id)
    assert adapter.session_count == 0
    with pytest.raises(module.ResidentSessionError):
        await _c2_create(files)
    assert adapter.session_count == 0


@pytest.mark.parametrize("bad", ["utc-back", "mono-back", "naive", "nan", "bool"])
async def test_t011_c_session_bad_clock_closes(c2_store: dict[str, Any], bad: str) -> None:
    session = await _c2_create(c2_store)
    if bad == "utc-back":
        c2_store["wall"].set(session.issued_at - timedelta(seconds=1))
    elif bad == "naive":
        c2_store["wall"].set(session.issued_at.replace(tzinfo=None))
    else:
        c2_store["tick"][0] = {"mono-back": 999.0, "nan": float("nan"), "bool": True}[bad]
    with pytest.raises(c2_store["load_module"]().ResidentAdmissionError) as caught:
        await _c2_validate(c2_store, session.session_id)
    _c1_error(caught.value, 503)
    assert c2_store["adapter"].session_count == 0
    c2_store["wall"].set(session.issued_at)
    c2_store["tick"][0] = 1000.0
    with pytest.raises(c2_store["load_module"]().ResidentAdmissionError):
        await _c2_create(c2_store)


async def test_t011_c_session_capacity_concurrent_and_prune(c2_store: dict[str, Any]) -> None:
    first = await _c2_create(c2_store)
    adapter = c2_store["adapter"]
    for _ in range(62):
        await _c2_create(c2_store)
    results: list[Any] = []
    module = c2_store["load_module"]()

    async def attempt() -> None:
        try:
            results.append(await _c2_create(c2_store, "other"))
        except module.ResidentSessionError as error:
            results.append(error)

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(attempt)
    failures = [item for item in results if isinstance(item, module.ResidentSessionError)]
    assert len(failures) == 2 and all(item.status_code == 429 for item in failures)
    assert all(item.code == ErrorCode.SAFETY_LIMIT_EXCEEDED for item in failures)
    assert adapter.session_count == 64
    assert await _c2_validate(c2_store, first.session_id) == first, (
        "capacity must not evict live entries"
    )
    c2_store["wall"].set(first.expires_at)
    assert adapter.prune_expired() == 64 and adapter.session_count == 0
    assert adapter.prune_expired() == 0
    await _c2_create(c2_store)
    assert adapter.session_count == 1


@pytest.mark.parametrize(
    "change", ["peer-reload", "resident-reload", "close", "c1-close", "failed-reload"]
)
async def test_t011_c_session_lifecycle(c2_store: dict[str, Any], change: str) -> None:
    from domoai.mcp.token_lifecycle import TokenFileManager

    session = await _c2_create(c2_store)
    if change in {"peer-reload", "resident-reload"}:
        name = "mcp" if change == "peer-reload" else "resident"
        TokenFileManager(c2_store["paths"][name]).rotate(name, scopes=["manage"])
        c2_store["verifiers"][name].reload()
    elif change == "c1-close":
        c2_store["admission"].close()
    else:
        snapshots = [v.credential_snapshot() for v in c2_store["verifiers"].values()]
        c2_store["adapter"].close()
        c2_store["adapter"].close()
        assert c2_store["adapter"].session_count == 0
        assert [v.credential_snapshot() for v in c2_store["verifiers"].values()] == snapshots
        if change == "failed-reload":
            c2_store["paths"]["resident"].write_text("{", encoding="utf-8")
            with pytest.raises(ValueError):
                c2_store["verifiers"]["resident"].reload()
    with pytest.raises(c2_store["load_module"]().ResidentAdmissionError) as caught:
        await _c2_validate(c2_store, session.session_id)
    _c1_error(caught.value, 503)
    assert c2_store["adapter"].session_count == 0


@pytest.mark.parametrize("action", ["close", "cancel"])
async def test_t011_c_session_close_while_real_verifier_waits(
    c2_store: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    entered, release = anyio.Event(), anyio.Event()
    original = StaticBearerTokenVerifier.verify_token
    results: list[Any] = []

    async def paused(self: StaticBearerTokenVerifier, token: str) -> AccessToken | None:
        entered.set()
        await release.wait()
        return await original(self, token)

    async def create() -> None:
        try:
            results.append(await _c2_create(c2_store))
        except c2_store["load_module"]().ResidentAdmissionError as error:
            results.append(error)

    monkeypatch.setattr(StaticBearerTokenVerifier, "verify_token", paused)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(create)
            await entered.wait()
            try:
                if action == "close":
                    c2_store["adapter"].close()
                else:
                    group.cancel_scope.cancel()
            finally:
                release.set()
    assert c2_store["adapter"].session_count == 0
    if action == "close":
        assert len(results) == 1
        _c1_error(results[0], 503)
    else:
        assert results == []


@pytest.mark.parametrize("fault", ["collision", "entropy"])
async def test_t011_c_session_entropy_failure_closes(
    c2_store: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import secrets

    session = await _c2_create(c2_store)
    calls: list[int] = []

    def broken(nbytes: int) -> str:
        calls.append(nbytes)
        if fault == "entropy":
            raise OSError("entropy unavailable")
        assert isinstance(session.session_id, str)
        return session.session_id

    monkeypatch.setattr(secrets, "token_urlsafe", broken)
    with pytest.raises(c2_store["load_module"]().ResidentAdmissionError) as caught:
        await _c2_create(c2_store)
    _c1_error(caught.value, 503)
    assert len(calls) == (3 if fault == "collision" else 1)
    assert c2_store["adapter"].session_count == 0


def test_t011_c_session_rejects_fake_admission() -> None:
    import domoai.management.consent as module

    invalid: Any = object()
    with pytest.raises(ValueError):
        module.ResidentConsentAdapter(admission=invalid)


# --- T011-C3 RED: POST /api/v1/resident/reviews (freeze p04-t011-c3-freeze.md + R1/R2) -------


class _C3Source:
    """Read-only review source; any other attribute access is a boundary violation."""

    def __init__(self, files: dict[str, Any]) -> None:
        self.files = files
        self.targets: dict[str, Any] = {}
        self.calls: list[tuple[Any, Any]] = []
        self.hook: Any = None
        self.mode = "ok"
        self.capability = "switch"
        self.area: str | None = "room-a"

    def __getattr__(self, name: str) -> Any:
        raise AssertionError("review source crossed read-only boundary: " + name)

    async def load(self, identity: Any, reference: Any) -> Any:
        self.calls.append((identity, reference))
        if self.hook is not None:
            await self.hook()
        if self.mode == "raises":
            raise RuntimeError("secret internal failure")
        if self.mode == "value-error":
            raise ValueError("clarification_required: from source")
        target = self.targets.get(reference.id)
        if target is None:
            return None
        if self.mode == "other-target":
            target = self.targets["plan-other"]
        return _c3_input(target, self.capability, self.area)


def _c3_input(target: Any, capability: str = "switch", area: str | None = "room-a") -> Any:
    from domoai.domain.household_preferences import PreferenceProfile, PreferenceVersion
    from domoai.management.consent import ConsentReviewInput

    plan = getattr(target, "plan_template", target)
    return ConsentReviewInput(
        target=target,
        devices=[_c3_device(command.device_id, capability, area) for command in plan.commands],
        preferences=PreferenceProfile(
            version=PreferenceVersion(
                generation="00000000-0000-4000-8000-000000000002", revision=7
            ),
            observed_at=plan.created_at,
            next_transition_at=None,
            preferences=[],
            next_cursor=None,
        ),
        control_version={"generation": "00000000-0000-4000-8000-000000000004", "revision": 3},
        timezone="Europe/Madrid",
    )


def _c3_device(device_id: str, capability: str = "switch", area: str | None = "room-a") -> Any:
    from domoai.domain.models import Device

    return Device.model_validate(
        {
            "id": device_id,
            "area_id": area,
            "name": "Luz salón",
            "type": "light",
            "protocol": "fixture",
            "source_refs": [{"adapter_id": "fixture", "external_id": device_id}],
            "capabilities": [
                {
                    "name": capability,
                    "kind": "number",
                    "readable": True,
                    "writable": True,
                    "minimum": 0,
                    "maximum": 1,
                    "commands": ["switch.set"],
                }
            ],
        }
    )


def _c3_plan_dict(
    now: datetime,
    plan_id: str = "plan-a",
    *,
    device: str = "light-a",
    plan_ttl: int = 7200,
    valid_ttl: int = 7200,
    tenant: str = "tenant-a",
    household: str = "home-a",
    unit: str | None = None,
) -> dict[str, Any]:
    return {
        "id": plan_id,
        "authority": AuthorityContext(
            tenant_id=tenant,
            household_id=household,
            household_ids=[household],
            principal_id="planner-a",
            roles=["planner"],
        ),
        "created_at": now,
        "expires_at": now + timedelta(seconds=plan_ttl),
        "execute_at": now,
        "execution_window": {
            "intended_at": now,
            "not_before": now,
            "not_after": now + timedelta(seconds=7200),
            "timezone": "Europe/Madrid",
            "revision": 2,
        },
        "schedule_revision": 2,
        "commands": [
            {
                "id": f"{plan_id}-cmd",
                "device_id": device,
                "command": "switch.set",
                "value": 1,
                "unit": unit,
                "risk_class": "confirm",
                "idempotency_key": f"{plan_id}-once",
            }
        ],
        "status": "requires_confirmation",
        "policy_decisions": [
            {
                "policy_id": "confirm",
                "action": "confirm",
                "reason": "Requiere confirmación del residente",
                "allows_stale": False,
            }
        ],
        "validation": {
            "status": "requires_confirmation",
            "validated_at": now,
            "runtime_revision": "fixture-v1",
            "digest": "sha256:" + "1" * 64,
            "valid_until": now + timedelta(seconds=valid_ttl),
        },
    }


def _c3_plan(now: datetime, plan_id: str = "plan-a", **changes: Any) -> Any:
    from domoai.domain.models import Plan

    return Plan.model_validate(_c3_plan_dict(now, plan_id, **changes))


def _c3_rule(
    now: datetime, rule_id: str = "rule-a", *, ttl: int = 7200, trigger_device: str = "light-a"
) -> Any:
    from domoai.domain.automation import AutomationRule

    template = _c3_plan_dict(now, "rule-template")
    template.update(status="draft", validation=None, execute_at=None, execution_window=None)
    return AutomationRule.model_validate(
        {
            "id": rule_id,
            "name": "Mantener luz",
            "authority": template["authority"],
            "scope": "salon-light",
            "plan_template": template,
            "trigger": {
                "type": "state_changed",
                "device_id": trigger_device,
                "capability": "switch",
                "expected": 1,
            },
            "conditions": [
                {
                    "device_id": "light-a",
                    "capability": "switch",
                    "operator": "not_equals",
                    "expected": 0,
                }
            ],
            "cooldown_seconds": 3600,
            "expires_at": now + timedelta(seconds=ttl),
        }
    )


def _c3_canonical(view: Any) -> bytes:
    return json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _c3_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class _C3Env:
    def __init__(self, files: dict[str, Any], source: _C3Source | None, adapter: Any) -> None:
        self.files, self.source, self.adapter = files, source, adapter
        self.client: Any = None
        self.now = files["wall"].now()
        self.session_id = ""

    def headers(self, **changes: Any) -> dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.files["tokens"]["resident"],
            "Origin": "https://home.example",
            "X-Resident-Session": self.session_id,
            "Content-Type": "application/json",
        }
        headers.update(changes)
        return {key: value for key, value in headers.items() if value is not None}

    async def new_session(self, actor: str = "resident") -> str:
        created = await self.client.post(
            "/api/v1/resident/session",
            headers={
                "Authorization": "Bearer " + self.files["tokens"][actor],
                "Origin": "https://home.example",
            },
        )
        assert created.status_code == 201, created.text
        return str(created.json()["session_id"])

    async def review(self, reference: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        body = {"reference": reference or {"kind": "plan", "id": "plan-a"}}
        kwargs.setdefault("headers", self.headers())
        if "content" not in kwargs:
            kwargs["content"] = json.dumps(body)
        return await self.client.post("/api/v1/resident/reviews", **kwargs)


@asynccontextmanager
async def _c3_open(
    files: dict[str, Any],
    *,
    with_source: bool = True,
    rate_limit: int | None = None,
    approval_store: Any = None,
) -> Any:
    import domoai.management.consent as module

    source = _C3Source(files) if with_source else None
    options: dict[str, Any] = {"review_source": source} if with_source else {}
    if approval_store is not None:
        options["approval_store"] = approval_store
    if source is not None:
        source.targets["plan-a"] = _c3_plan(files["wall"].now())
    adapter = module.ResidentConsentAdapter(
        admission=files["admission"],
        clock=files["wall"],
        monotonic=lambda: files["tick"][0],
        **options,
    )
    app_options: dict[str, Any] = {} if rate_limit is None else {"rate_limit": rate_limit}
    app = create_management_app(StubManager(), resident_consent=adapter, **app_options)
    env = _C3Env(files, source, adapter)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://home.example"
        ) as client:
            env.client = client
            env.session_id = await env.new_session()
            yield env


@pytest.fixture
async def c3(c2_store: dict[str, Any]) -> Any:
    async with _c3_open(c2_store) as env:
        yield env


def _c3_secure(response: Any) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert not {"set-cookie", "access-control-allow-origin", "location"}.intersection(
        response.headers
    )


async def test_t011_c3_review_positive_view_digest_and_challenge(
    c3: _C3Env, caplog: pytest.LogCaptureFixture
) -> None:
    import re

    import domoai.management.consent as module
    from domoai.application.household_consent import build_consent_view
    from domoai.domain.household_consent import ConsentView

    response = await c3.review()
    assert response.status_code == 200, response.text
    _c3_secure(response)
    payload = response.json()
    assert set(payload) == {"view", "review_digest", "challenge_id", "challenge_expires_at"}
    view = ConsentView.model_validate(payload["view"])
    assert view.permission_status == "awaiting_review"
    assert (view.tenant_id, view.household_id) == ("tenant-a", "home-a")
    loaded = _c3_input(c3.source.targets["plan-a"])
    expected_view = build_consent_view(
        target=loaded.target,
        devices=loaded.devices,
        preferences=loaded.preferences,
        control_version=loaded.control_version,
        now=c3.now,
        timezone=loaded.timezone,
    )
    assert payload["view"] == expected_view.model_dump(mode="json")
    assert payload["review_digest"] == (
        "sha256:" + hashlib.sha256(_c3_canonical(payload["view"])).hexdigest()
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", payload["challenge_id"])
    assert payload["challenge_expires_at"] == _c3_z(c3.now + timedelta(seconds=120))
    for secret in (c3.session_id, c3.files["tokens"]["resident"]):
        assert secret not in response.text
    assert [(call[0].tenant_id, call[1].kind, call[1].id) for call in c3.source.calls] == [
        ("tenant-a", "plan", "plan-a")
    ]
    (challenge,) = c3.adapter.challenges
    assert isinstance(challenge, module.ResidentChallenge)
    assert challenge.challenge_id == payload["challenge_id"]
    assert challenge.session_id == c3.session_id
    assert challenge.purpose == "approve"
    assert (challenge.reference.kind, challenge.reference.id) == ("plan", "plan-a")
    assert challenge.review_digest == payload["review_digest"]
    assert challenge.expires_at == c3.now + timedelta(seconds=120)
    for secret in (payload["challenge_id"], c3.session_id):
        assert secret not in repr(challenge) and secret not in repr(c3.adapter)
        assert secret not in caplog.text
    assert c3.adapter.challenge_count == 1
    decisions = await c3.client.post(
        "/api/v1/resident/decisions",
        headers=c3.headers(),
        content=json.dumps(
            {
                "challenge_id": "x",
                "action": "approve",
                "expected_review_digest": "sha256:" + "0" * 64,
                "request_id": "r",
            }
        ),
    )
    assert decisions.status_code == 503
    assert (await c3.client.get("/api/v1/resident/reviews")).status_code == 405


async def test_t011_c3_rule_review_is_supported(c3: _C3Env) -> None:
    from domoai.domain.household_consent import ConsentView

    c3.source.targets["rule-a"] = _c3_rule(c3.now)
    response = await c3.review({"kind": "rule", "id": "rule-a"})
    assert response.status_code == 200, response.text
    view = ConsentView.model_validate(response.json()["view"])
    assert view.reference.kind == "rule" and view.constraints is not None
    assert view.window.not_before == c3.now
    assert response.json()["challenge_expires_at"] == _c3_z(c3.now + timedelta(seconds=120))
    (challenge,) = c3.adapter.challenges
    assert (challenge.reference.kind, challenge.reference.id) == ("rule", "rule-a")


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"X-Resident-Session": None}, 401),
        ({"X-Resident-Session": ""}, 401),
        ({"X-Resident-Session": "unknown"}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": None}, 401),
        ({"Origin": "https://attacker.example"}, 403),
        ({"Origin": None}, 403),
    ],
)
@pytest.mark.parametrize(
    "body",
    [
        {"content": "not json"},
        {"headers": {"Content-Type": "text/plain"}, "content": "x"},
        {"content": b'{"reference":{"kind":"plan","id":"' + b"a" * 5_000_000 + b'"}}'},
        {"params": {"x": "1"}},
    ],
)
async def test_t011_c3_review_admission_precedes_body_and_source(
    c3: _C3Env, changes: dict[str, Any], status: int, body: dict[str, Any]
) -> None:
    extra = dict(body.get("headers", {}), **changes)
    kwargs = dict(body, headers=c3.headers(**extra))
    response = await c3.review(**kwargs)
    assert response.status_code == status
    assert (response.headers.get("www-authenticate") == "Bearer") is (status == 401)
    assert c3.source.calls == [] and c3.adapter.challenge_count == 0


@pytest.mark.parametrize(
    "name,second",
    [
        ("X-Resident-Session", "same"),
        ("X-Resident-Session", "unknown"),
        ("Authorization", "same"),
        ("Origin", "same"),
    ],
)
async def test_t011_c3_review_duplicate_credential_headers_are_rejected(
    c3: _C3Env, name: str, second: str
) -> None:
    headers = c3.headers()
    value = headers[name] if second == "same" else second
    response = await c3.review(headers=[*headers.items(), (name, value)])
    assert response.status_code in {401, 403}, response.text
    assert c3.source.calls == [] and c3.adapter.challenge_count == 0


async def test_t011_c3_other_identity_bearer_cannot_use_the_session(c3: _C3Env) -> None:
    other = c3.headers(Authorization="Bearer " + c3.files["tokens"]["other"])
    response = await c3.review(headers=other)
    assert response.status_code == 401 and c3.source.calls == []


@pytest.mark.parametrize(
    "kwargs,status",
    [
        ({"headers": {"Content-Type": "text/plain"}}, 415),
        ({"headers": {"Content-Type": "application/json; charset=utf-8"}}, 415),
        ({"headers": {"Content-Type": None}}, 415),
        ({"headers": {"Content-Type": "text/plain"}, "params": {"x": "1"}}, 400),
        ({"headers": {"Content-Type": "text/plain", "Transfer-Encoding": "chunked"}}, 415),
        ({"headers": {"Transfer-Encoding": "chunked"}}, 400),
        ({"headers": {"Content-Length": "abc"}}, 400),
        ({"content": "{"}, 400),
        ({"content": "[]"}, 400),
        ({"content": '{"reference":{"kind":"plan","id":"plan-a"},"tenant_id":"x"}'}, 400),
        ({"content": '{"reference":{"kind":"plan","id":"plan-a","extra":1}}'}, 400),
        ({"content": '{"reference":{"kind":"device","id":"plan-a"}}'}, 400),
        ({"content": '{"reference":{"kind":"plan","id":""}}'}, 400),
        ({"content": '{"reference":{"kind":"plan","id":"' + "a" * 201 + '"}}'}, 400),
        ({"content": '{"reference":{"kind":"plan","id":1}}'}, 400),
        ({"content": '{"reference":{"kind":"plan","id":null}}'}, 400),
        ({"content": '{"reference":{"kind":"plan","id":"plan-a","id":"plan-b"}}'}, 400),
        ({"content": "{}"}, 400),
        ({"content": '{"reference":null}'}, 400),
        ({"content": '{"reference":[]}'}, 400),
        ({"content": '{"reference":{"kind":"plan"}}'}, 400),
        ({"content": '{"reference":{"id":"plan-a"}}'}, 400),
        ({"content": '{"reference":{"kind":"bundle","id":"plan-a"}}'}, 400),
        ({"content": '{"reference":{"kind":"recurrence","id":"plan-a"}}'}, 400),
        ({"headers": {"Content-Length": "2, 2"}}, 400),
        (
            {
                "headers": {"Transfer-Encoding": "chunked"},
                "content": b'{"reference":{"kind":"plan","id":"' + b"a" * 5_000_000 + b'"}}',
            },
            400,
        ),
        ({"content": b"\xef\xbb\xbf" + b'{"reference":{"kind":"plan","id":"plan-a"}}'}, 400),
        ({"params": {"x": "1"}}, 400),
        ({"content": b'{"reference":{"kind":"plan","id":"' + b"a" * 5_000_000 + b'"}}'}, 413),
    ],
)
async def test_t011_c3_review_body_is_strict_and_read_after_auth(
    c3: _C3Env, kwargs: dict[str, Any], status: int
) -> None:
    if "headers" in kwargs:
        kwargs = dict(kwargs, headers=c3.headers(**kwargs["headers"]))
    response = await c3.review(**kwargs)
    assert response.status_code == status, response.text
    _c3_secure(response)
    assert c3.source.calls == [] and c3.adapter.challenge_count == 0


async def test_t011_c3_review_slow_body_is_408(c3: _C3Env) -> None:
    body = json.dumps({"reference": {"kind": "plan", "id": "plan-a"}}).encode()

    async def slow() -> Any:
        await anyio.sleep(3)
        yield body

    response = await c3.review(
        content=slow(), headers=c3.headers(**{"Content-Length": str(len(body))})
    )
    assert response.status_code == 408, response.text
    assert c3.source.calls == [] and c3.adapter.challenge_count == 0


async def test_t011_c3_close_during_body_wins_over_a_valid_body(c3: _C3Env) -> None:
    body = json.dumps({"reference": {"kind": "plan", "id": "plan-a"}}).encode()

    async def closing() -> Any:
        yield body
        c3.adapter.close()

    response = await c3.review(
        content=closing(), headers=c3.headers(**{"Content-Length": str(len(body))})
    )
    assert response.status_code == 503, response.text
    assert c3.source.calls == [] and c3.adapter.challenge_count == 0


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "foreign-tenant",
        "foreign-household",
        "acl-device",
        "acl-capability",
        "acl-area",
        "acl-area-unknown",
        "other-target",
        "raises",
        "value-error",
    ],
)
async def test_t011_c3_review_unresolvable_target_is_one_generic_404(c3: _C3Env, case: str) -> None:
    if case == "foreign-tenant":
        c3.source.targets["plan-a"] = _c3_plan(c3.now, tenant="tenant-b")
    elif case == "foreign-household":
        c3.source.targets["plan-a"] = _c3_plan(c3.now, household="home-b")
    elif case == "acl-device":
        c3.source.targets["plan-a"] = _c3_plan(c3.now, device="heater-a")
    elif case == "acl-capability":
        c3.source.capability = "dimmer"
    elif case == "acl-area":
        c3.source.area = "room-b"
    elif case == "acl-area-unknown":
        c3.source.area = None
    elif case == "other-target":
        c3.source.targets["plan-other"] = _c3_plan(c3.now, "plan-other")
        c3.source.mode = case
    elif case in {"raises", "value-error"}:
        c3.source.mode = case
    else:
        c3.source.targets.clear()
    response = await c3.review()
    assert response.status_code == 404, response.text
    assert response.json() == {"error": "not_found", "message": "No se encontró el objetivo"}
    assert "secret internal failure" not in response.text and "plan-a" not in response.text
    assert c3.adapter.challenge_count == 0


async def test_t011_c3_review_source_timeout_is_404(c3: _C3Env) -> None:
    async def never() -> None:
        await anyio.sleep(30)

    c3.source.hook = never
    with anyio.fail_after(10):
        response = await c3.review()
    assert response.status_code == 404 and c3.adapter.challenge_count == 0


@pytest.mark.parametrize("case", ["unit", "plan-expired", "validation-expired"])
async def test_t011_c3_review_needing_clarification_is_422_without_challenge(
    c3: _C3Env, case: str
) -> None:
    if case == "unit":
        c3.source.targets["plan-a"] = _c3_plan(c3.now, unit="degC")
    elif case == "plan-expired":
        c3.source.targets["plan-a"] = _c3_plan(c3.now, plan_ttl=0)
    else:
        c3.source.targets["plan-a"] = _c3_plan(c3.now, valid_ttl=0)
    response = await c3.review()
    assert response.status_code == 422, response.text
    assert response.json() == {
        "error": "clarification_required",
        "message": "La revisión requiere aclaración",
    }
    assert c3.adapter.challenge_count == 0


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("secret projection failure"),
        ValueError("secret other value error"),
        ValueError("xclarification_required: secret"),
        ValueError("secret clarification_required: embedded"),
    ],
)
async def test_t011_c3_review_projection_other_errors_are_500_generic(
    c3: _C3Env, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    import domoai.management.consent as module

    def boom(**_: Any) -> Any:
        raise error

    monkeypatch.setattr(module, "build_consent_view", boom, raising=False)
    response = await c3.review()
    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert "secret" not in response.text and c3.adapter.challenge_count == 0


async def test_t011_c3_review_projection_validation_error_is_500_generic(
    c3: _C3Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import domoai.management.consent as module
    from domoai.domain.household_consent import ConsentView

    def boom(**_: Any) -> Any:
        return ConsentView.model_validate({"secret": "invalid"})

    monkeypatch.setattr(module, "build_consent_view", boom, raising=False)
    response = await c3.review()
    assert response.status_code == 500 and response.json() == {"error": "internal_error"}
    assert "secret" not in response.text and c3.adapter.challenge_count == 0


@pytest.mark.parametrize("limit", ["session", "plan", "validation"])
async def test_t011_c3_challenge_ttl_is_bounded_by_session_and_view(c3: _C3Env, limit: str) -> None:
    now = c3.now
    if limit == "session":
        c3.files["wall"].set(now + timedelta(seconds=540))
        c3.files["tick"][0] += 540
        c3.source.targets["plan-a"] = _c3_plan(c3.files["wall"].now())
        expected = now + timedelta(seconds=600)
    elif limit == "plan":
        c3.source.targets["plan-a"] = _c3_plan(now, plan_ttl=60, valid_ttl=7200)
        expected = now + timedelta(seconds=60)
    else:
        c3.source.targets["plan-a"] = _c3_plan(now, plan_ttl=7200, valid_ttl=30)
        expected = now + timedelta(seconds=30)
    response = await c3.review()
    assert response.status_code == 200, response.text
    assert response.json()["challenge_expires_at"] == _c3_z(expected)
    assert c3.adapter.challenges[0].expires_at == expected


async def test_t011_c3_challenge_replacement_cap_and_expiry(c3: _C3Env) -> None:
    for name in ("plan-b", "plan-c", "plan-d", "plan-e"):
        c3.source.targets[name] = _c3_plan(c3.now, name)
    first = (await c3.review()).json()
    again = (await c3.review()).json()
    assert again["challenge_id"] != first["challenge_id"] and c3.adapter.challenge_count == 1
    assert [item.challenge_id for item in c3.adapter.challenges] == [again["challenge_id"]]
    for name in ("plan-b", "plan-c", "plan-d"):
        assert (await c3.review({"kind": "plan", "id": name})).status_code == 200
    assert c3.adapter.challenge_count == 4
    over = await c3.review({"kind": "plan", "id": "plan-e"})
    assert over.status_code == 429 and over.json() == {"error": "rate_limited"}
    assert c3.adapter.challenge_count == 4
    assert (await c3.review()).status_code == 200 and c3.adapter.challenge_count == 4
    other = await c3.new_session()
    second = await c3.review(
        {"kind": "plan", "id": "plan-e"}, headers=c3.headers(**{"X-Resident-Session": other})
    )
    assert second.status_code == 200, "the cap is per session"
    assert c3.adapter.challenge_count == 5
    c3.files["wall"].set(c3.now + timedelta(seconds=121))
    c3.files["tick"][0] += 121
    assert c3.adapter.prune_expired() >= 0
    assert c3.adapter.session_count == 2 and c3.adapter.challenge_count == 0


async def test_t011_c3_expired_challenges_do_not_count_toward_the_cap(c3: _C3Env) -> None:
    names = ("plan-b", "plan-c", "plan-d", "plan-e")
    for name in names:
        c3.source.targets[name] = _c3_plan(c3.now, name)
    for name in ("plan-a", *names[:3]):
        assert (await c3.review({"kind": "plan", "id": name})).status_code == 200
    c3.files["wall"].set(c3.now + timedelta(seconds=121))
    c3.files["tick"][0] += 121
    for name in names:
        c3.source.targets[name] = _c3_plan(c3.files["wall"].now(), name)
    fresh = await c3.review({"kind": "plan", "id": "plan-e"})
    assert fresh.status_code == 200, fresh.text
    assert c3.adapter.challenge_count == 1


async def test_t011_c3_close_during_load_creates_no_challenge(c3: _C3Env) -> None:
    async def close() -> None:
        c3.adapter.close()

    c3.source.hook = close
    response = await c3.review()
    assert response.status_code == 503
    assert c3.adapter.challenge_count == 0


async def test_t011_c3_session_expiry_during_load_creates_no_challenge(c3: _C3Env) -> None:
    async def expire() -> None:
        c3.files["wall"].set(c3.now + timedelta(seconds=601))
        c3.files["tick"][0] += 601

    c3.source.hook = expire
    response = await c3.review()
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    assert c3.adapter.challenge_count == 0


async def test_t011_c3_credential_reload_during_load_closes_without_challenge(
    c3: _C3Env,
) -> None:
    from domoai.mcp.token_lifecycle import TokenFileManager

    async def rotate() -> None:
        TokenFileManager(c3.files["paths"]["resident"]).rotate(
            "resident-extra",
            scopes=["consent:human"],
            tenant_id="tenant-a",
            household_ids=["home-a"],
            roles=[PrincipalRole.OPERATOR],
            operations=["review_consent"],
        )
        c3.files["verifiers"]["resident"].reload()

    c3.source.hook = rotate
    response = await c3.review()
    assert response.status_code == 503, response.text
    assert c3.adapter.challenge_count == 0
    c3.source.hook = None
    assert (await c3.review()).status_code == 503


async def test_t011_c3_closed_host_rejects_review_before_any_source_call(c3: _C3Env) -> None:
    c3.adapter.close()
    response = await c3.review()
    assert response.status_code == 503 and c3.source.calls == []
    assert c3.adapter.challenge_count == 0


async def test_t011_c3_expired_session_prunes_its_challenges(c3: _C3Env) -> None:
    assert (await c3.review()).status_code == 200 and c3.adapter.challenge_count == 1
    c3.files["wall"].set(c3.now + timedelta(seconds=601))
    c3.files["tick"][0] += 601
    c3.adapter.prune_expired()
    assert c3.adapter.session_count == 0 and c3.adapter.challenge_count == 0


async def test_t011_c3_close_clears_challenges(c3: _C3Env) -> None:
    assert (await c3.review()).status_code == 200 and c3.adapter.challenge_count == 1
    c3.adapter.close()
    assert c3.adapter.challenge_count == 0 and c3.adapter.challenges == ()


async def test_t011_c3_shared_resident_quota_applies_to_reviews(
    c2_store: dict[str, Any],
) -> None:
    async with _c3_open(c2_store, rate_limit=3) as env:
        assert (await env.review()).status_code == 200
        assert (await env.review()).status_code == 200
        limited = await env.review()
        assert limited.status_code == 429 and limited.json() == {"error": "rate_limited"}
        assert len(env.source.calls) == 2


async def test_t011_c3_missing_review_source_is_unavailable_after_session_auth(
    c2_store: dict[str, Any],
) -> None:
    async with _c3_open(c2_store, with_source=False) as env:
        response = await env.review()
        assert response.status_code == 503, response.text
        assert response.json()["error"] == "human_confirmation_unavailable"
        bad = await env.review(headers=env.headers(**{"X-Resident-Session": "unknown"}))
        assert bad.status_code == 401
        assert env.adapter.challenge_count == 0


def test_t011_c3_adapter_rejects_nonconcrete_review_source(c2_store: dict[str, Any]) -> None:
    import domoai.management.consent as module

    for bad in (object(), "source", 1):
        with pytest.raises(ValueError):
            module.ResidentConsentAdapter(admission=c2_store["admission"], review_source=bad)


async def test_t011_c3_unconfigured_review_keeps_unavailable_contract() -> None:
    app = create_management_app(StubManager())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://home.example"
    ) as client:
        response = await client.post("/api/v1/resident/reviews")
    assert response.status_code == 503
    assert response.json()["error"] == "human_confirmation_unavailable"


async def test_t011_c3_rule_acl_and_reference_mismatch_are_generic_404(c3: _C3Env) -> None:
    rule = _c3_rule(c3.now)
    foreign = rule.model_copy(
        update={
            "plan_template": rule.plan_template.model_copy(
                update={
                    "commands": [
                        rule.plan_template.commands[0].model_copy(update={"device_id": "heater-a"})
                    ]
                }
            )
        }
    )
    c3.source.targets["rule-a"] = foreign
    for reference in ({"kind": "rule", "id": "rule-a"}, {"kind": "plan", "id": "rule-a"}):
        response = await c3.review(reference)
        assert response.status_code == 404, response.text
        assert response.json() == {"error": "not_found", "message": "No se encontró el objetivo"}
    c3.source.targets["rule-a"] = rule
    mismatch = await c3.review({"kind": "plan", "id": "rule-a"})
    assert mismatch.status_code == 404 and c3.adapter.challenge_count == 0


async def test_t011_c3_rule_trigger_device_outside_acl_is_404(c3: _C3Env) -> None:
    c3.source.targets["rule-a"] = _c3_rule(c3.now, trigger_device="heater-a")
    response = await c3.review({"kind": "rule", "id": "rule-a"})
    assert response.status_code == 404, response.text
    assert c3.adapter.challenge_count == 0


async def test_t011_c3_credential_reload_then_source_failure_is_503(c3: _C3Env) -> None:
    from domoai.mcp.token_lifecycle import TokenFileManager

    async def rotate() -> None:
        TokenFileManager(c3.files["paths"]["resident"]).rotate(
            "resident-extra",
            scopes=["consent:human"],
            tenant_id="tenant-a",
            household_ids=["home-a"],
            roles=[PrincipalRole.OPERATOR],
            operations=["review_consent"],
        )
        c3.files["verifiers"]["resident"].reload()

    c3.source.hook = rotate
    c3.source.mode = "raises"
    response = await c3.review()
    assert response.status_code == 503, response.text
    assert c3.adapter.challenge_count == 0
    c3.source.hook = None
    assert (await c3.review()).status_code == 503


# --- T011-C4 RED: POST /api/v1/resident/decisions (freeze p04-t011-c4-freeze.md + R1) --------

_C4_BAD_DIGEST = "sha256:" + "0" * 64
_C4_CONFLICT = {
    "error": "challenge_invalid",
    "message": "El desafío no es válido o expiró; revisa de nuevo",
}


@pytest.fixture
async def c4_store(
    c1_files: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    from domoai.mcp.token_lifecycle import TokenFileManager
    from domoai.persistence.repositories import ApprovalGrantRepository
    from domoai.persistence.sqlite import SQLiteDatabase
    from domoai.runtime.approval_store import ApprovalStore
    from domoai.runtime.clock import FixedClock

    monkeypatch.undo()  # c1_files forbids the real store; C4 exercises it for real.
    _c1_change_record(c1_files, operations=["review_consent", "approve_consent"])
    other = TokenFileManager(c1_files["paths"]["resident"]).rotate(
        "resident-other",
        scopes=["consent:human"],
        tenant_id="tenant-a",
        household_ids=["home-a"],
        roles=[PrincipalRole.OPERATOR],
        operations=["review_consent", "approve_consent"],
    )
    c1_files["tokens"]["other"] = other.token
    c1_files["verifiers"]["resident"].reload()
    wall, tick = FixedClock(datetime.now(UTC)), [1000.0]
    database = SQLiteDatabase(tmp_path / "c4.sqlite3", clock=wall)
    await database.initialize()
    repository = ApprovalGrantRepository(database)
    store = ApprovalStore(clock=wall, persistence=repository)
    try:
        yield dict(
            c1_files,
            admission=_c1_admission(c1_files),
            wall=wall,
            tick=tick,
            store=store,
            repository=repository,
        )
    finally:
        await database.close()


@pytest.fixture
async def c4(c4_store: dict[str, Any]) -> Any:
    async with _c3_open(c4_store, approval_store=c4_store["store"]) as env:
        env.store, env.repository = c4_store["store"], c4_store["repository"]
        yield env


async def _c4_review(env: _C3Env, reference: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await env.review(reference)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _c4_decide(
    env: _C3Env,
    payload: dict[str, Any],
    *,
    action: str = "approve",
    digest: str | None = None,
    request_id: str = "req-1",
    **kwargs: Any,
) -> Any:
    body = {
        "challenge_id": payload["challenge_id"],
        "action": action,
        "expected_review_digest": digest or payload["review_digest"],
        "request_id": request_id,
    }
    kwargs.setdefault("headers", env.headers())
    kwargs.setdefault("content", json.dumps(body))
    return await env.client.post("/api/v1/resident/decisions", **kwargs)


def _c4_nonce(challenge_id: str) -> str:
    return hashlib.sha256(("resident-consent:" + challenge_id).encode()).hexdigest()


async def test_t011_c4_review_digest_has_the_contract_prefix(c4: _C3Env) -> None:
    payload = await _c4_review(c4)
    assert payload["review_digest"] == (
        "sha256:" + hashlib.sha256(_c3_canonical(payload["view"])).hexdigest()
    )


async def test_t011_c4_approve_issues_one_durable_server_derived_grant(
    c4: _C3Env, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.domain.models import Plan
    from domoai.runtime.approval_store import ApprovalStore

    caplog.set_level("DEBUG")
    seen: list[tuple[Any, Any, Any, dict[str, Any]]] = []
    original = ApprovalStore.issue_authenticated

    def spy(self: Any, plan: Any, *, principal: Any, assertion: Any = None, **kwargs: Any) -> Any:
        seen.append((plan, principal, assertion, kwargs))
        return original(self, plan, principal=principal, assertion=assertion, **kwargs)

    monkeypatch.setattr(ApprovalStore, "issue_authenticated", spy)
    payload = await _c4_review(c4)
    response = await _c4_decide(c4, payload)
    assert response.status_code == 200, response.text
    _c3_secure(response)
    body = response.json()
    assert set(body) == {
        "status",
        "approval_id",
        "expires_at",
        "review_digest",
        "reviewed_control_version",
    }
    assert body["status"] == "approved" and body["review_digest"] == payload["review_digest"]
    assert body["reviewed_control_version"] == payload["view"]["bindings"]["control_version"]
    grant = c4.repository.get_sync(body["approval_id"])
    assert grant is not None
    assert (grant.plan_id, grant.validation_digest) == ("plan-a", "sha256:" + "1" * 64)
    assert grant.approved_by == "resident" and grant.authentication_context == "resident_browser"
    assert grant.session_id == (
        "resident-session:" + hashlib.sha256(c4.session_id.encode()).hexdigest()[:32]
    )
    assert grant.assertion_nonce == _c4_nonce(payload["challenge_id"])
    assert grant.approved_at == c4.now and grant.expires_at == c4.now + timedelta(minutes=5)
    assert body["expires_at"] == _c3_z(grant.expires_at)
    assert (grant.authority.tenant_id, grant.authority.household_id) == ("tenant-a", "home-a")
    assert c4.repository.nonce_exists_sync(grant.assertion_nonce)
    assert c4.adapter.challenge_count == 0
    for secret in (
        payload["challenge_id"],
        c4.session_id,
        c4.files["tokens"]["resident"],
        grant.assertion_nonce,
    ):
        assert secret not in response.text and secret not in caplog.text
        assert secret not in repr(c4.adapter) and secret not in repr(grant.session_id)
    assert [call[1].id for call in c4.source.calls] == ["plan-a", "plan-a"]
    ((plan, principal, assertion, extra),) = seen
    assert type(plan) is Plan and plan.id == "plan-a" and set(extra) <= {"decision"}
    decision = extra.get("decision")
    assert decision is not None, "the receipt must be written with the grant (C5)"
    assert (decision.principal_id, decision.request_id) == ("resident", "req-1")
    assert payload["challenge_id"] not in repr(decision)
    assert assertion.principal is principal
    assert assertion.plan_id == "plan-a"
    assert assertion.validation_digest == "sha256:" + "1" * 64
    assert assertion.bundle_digest is None and assertion.recurrence_digest is None
    assert assertion.nonce == _c4_nonce(payload["challenge_id"])
    assert all(
        payload["challenge_id"] not in value and c4.session_id not in value
        for value in response.headers.values()
    )


async def test_t011_c4_challenge_is_single_use(c4: _C3Env) -> None:
    payload = await _c4_review(c4)
    assert (await _c4_decide(c4, payload)).status_code == 200
    # A *different* request id is a new decision on a consumed challenge, not a retry (C5).
    replay = await _c4_decide(c4, payload, request_id="req-replay")
    assert replay.status_code == 409 and replay.json() == _C4_CONFLICT
    again = await _c4_decide(c4, payload, request_id="req-2")
    assert again.status_code == 409 and again.json() == _C4_CONFLICT
    assert len(c4.source.calls) == 2


async def test_t011_c4_concurrent_decisions_emit_exactly_one_grant(c4: _C3Env) -> None:
    payload = await _c4_review(c4)
    results: list[Any] = []

    async def overlap() -> None:
        await anyio.sleep(0.05)

    c4.source.hook = overlap

    async def attempt(request_id: str) -> None:
        results.append(await _c4_decide(c4, payload, request_id=request_id))

    async with anyio.create_task_group() as group:
        for index in range(4):
            group.start_soon(attempt, f"req-{index}")
    assert sorted(response.status_code for response in results) == [200, 409, 409, 409]
    assert [r.json() for r in results if r.status_code == 409] == [_C4_CONFLICT] * 3
    assert len(c4.source.calls) == 2, "losers must be rejected before any reload"
    assert c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))


async def test_t011_c4_reject_consumes_without_a_grant_or_reload(c4: _C3Env) -> None:
    payload = await _c4_review(c4)
    response = await _c4_decide(c4, payload, action="reject")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "rejected", "review_digest": payload["review_digest"]}
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))
    assert len(c4.source.calls) == 1 and c4.adapter.challenge_count == 0
    later = await _c4_decide(c4, payload)
    assert later.status_code == 409 and later.json() == _C4_CONFLICT


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_t011_c4_deciding_requires_the_approve_consent_operation(
    c2_store: dict[str, Any], action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.runtime.approval_store import ApprovalStore

    monkeypatch.undo()  # c1_files forbids the real store; this one only needs to exist.
    store = ApprovalStore(clock=c2_store["wall"])
    async with _c3_open(c2_store, approval_store=store) as env:
        payload = await _c4_review(env)
        response = await _c4_decide(env, payload, action=action)
        assert response.status_code == 403, response.text
        assert response.json() == {
            "error": "insufficient_scope",
            "message": "La solicitud del residente no está autorizada",
        }
        assert env.adapter.challenge_count == 1 and len(env.source.calls) == 1


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"X-Resident-Session": None}, 401),
        ({"X-Resident-Session": "unknown"}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": None}, 401),
        ({"Origin": "https://attacker.example"}, 403),
        ({"Origin": None}, 403),
    ],
)
async def test_t011_c4_admission_precedes_body_and_consumption(
    c4: _C3Env, changes: dict[str, Any], status: int
) -> None:
    payload = await _c4_review(c4)
    response = await _c4_decide(c4, payload, headers=c4.headers(**changes), content="not json")
    assert response.status_code == status
    assert c4.adapter.challenge_count == 1 and len(c4.source.calls) == 1
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))


async def test_t011_c4_other_identity_bearer_cannot_decide(c4: _C3Env) -> None:
    payload = await _c4_review(c4)
    other = c4.headers(Authorization="Bearer " + c4.files["tokens"]["other"])
    response = await _c4_decide(c4, payload, headers=other)
    assert response.status_code == 401 and c4.adapter.challenge_count == 1


def _c4_real(payload: dict[str, Any], **changes: Any) -> str:
    body: dict[str, Any] = {
        "challenge_id": payload["challenge_id"],
        "action": "approve",
        "expected_review_digest": payload["review_digest"],
        "request_id": "req-1",
    }
    body.update(changes)
    return json.dumps(body)


def _c4_dup(payload: dict[str, Any]) -> str:
    cid, digest = payload["challenge_id"], payload["review_digest"]
    return (
        f'{{"challenge_id":"{cid}","challenge_id":"{cid}","action":"approve",'
        f'"expected_review_digest":"{digest}","request_id":"req-1"}}'
    )


@pytest.mark.parametrize(
    "spec,status",
    [
        ({"headers": {"Content-Type": "text/plain"}}, 415),
        ({"headers": {"Content-Type": None}}, 415),
        ({"headers": {"Content-Type": "text/plain"}, "params": {"x": "1"}}, 400),
        ({"headers": {"Transfer-Encoding": "chunked"}}, 400),
        ({"headers": {"Content-Length": "abc"}}, 400),
        ({"raw": lambda p: "{"}, 400),
        ({"raw": lambda p: "[]"}, 400),
        ({"raw": _c4_dup}, 400),
        ({"raw": lambda p: b"\xef\xbb\xbf" + _c4_real(p).encode()}, 400),
        ({"mutate": {"extra": 1}}, 400),
        ({"mutate": {"action": "revoke"}}, 403),  # C7: valid body, but no revoke_consent operation
        ({"mutate": {"action": "delete"}}, 400),
        ({"mutate": {"expected_review_digest": "abc"}}, 400),
        ({"mutate": {"expected_review_digest": "sha256:" + "A" * 64}}, 400),
        ({"mutate": {"challenge_id": ""}}, 400),
        ({"mutate": {"request_id": 1}}, 400),
        ({"mutate": {"tenant_id": "tenant-b"}}, 400),
        ({"params": {"x": "1"}}, 400),
        ({"raw": lambda p: b"x" * 5_000_000}, 413),
    ],
)
async def test_t011_c4_body_is_strict_and_never_consumes(
    c4: _C3Env, spec: dict[str, Any], status: int
) -> None:
    payload = await _c4_review(c4)
    raw = spec.get("raw")
    content = raw(payload) if raw else _c4_real(payload, **spec.get("mutate", {}))
    response = await c4.client.post(
        "/api/v1/resident/decisions",
        headers=c4.headers(**spec.get("headers", {})),
        params=spec.get("params"),
        content=content,
    )
    assert response.status_code == status, response.text
    _c3_secure(response)
    assert c4.adapter.challenge_count == 1 and len(c4.source.calls) == 1
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))
    # The untouched challenge is still usable afterwards.
    assert (await _c4_decide(c4, payload)).status_code == 200


async def test_t011_c4_without_an_approval_store_is_unavailable_and_keeps_the_challenge(
    c4_store: dict[str, Any],
) -> None:
    async with _c3_open(c4_store) as env:
        payload = await _c4_review(env)
        response = await _c4_decide(env, payload)
        assert response.status_code == 503, response.text
        assert response.json()["error"] == "human_confirmation_unavailable"
        assert env.adapter.challenge_count == 1
        bad = await _c4_decide(env, payload, content="{")
        assert bad.status_code == 400


def test_t011_c4_adapter_rejects_a_nonconcrete_approval_store(c4_store: dict[str, Any]) -> None:
    import domoai.management.consent as module
    from domoai.runtime.approval_store import ApprovalStore

    class Sub(ApprovalStore):
        pass

    for bad in (object(), "store", 1, Sub(clock=c4_store["wall"])):
        with pytest.raises(ValueError):
            module.ResidentConsentAdapter(admission=c4_store["admission"], approval_store=bad)


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("case", ["unknown", "other-session", "expired", "wrong-digest"])
async def test_t011_c4_invalid_challenges_share_one_conflict_body(
    c4: _C3Env, case: str, action: str
) -> None:
    payload = await _c4_review(c4)
    kwargs: dict[str, Any] = {"action": action}
    if case == "unknown":
        payload = dict(payload, challenge_id="unknown")
    elif case == "other-session":
        kwargs["headers"] = c4.headers(**{"X-Resident-Session": await c4.new_session()})
    elif case == "expired":
        c4.files["wall"].set(c4.now + timedelta(seconds=121))
        c4.files["tick"][0] += 121
    else:
        kwargs["digest"] = _C4_BAD_DIGEST
    response = await _c4_decide(c4, payload, **kwargs)
    assert response.status_code == 409, response.text
    assert response.json() == _C4_CONFLICT
    assert len(c4.source.calls) == 1
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))
    # A foreign session must not burn the owner's challenge; every other case consumes it.
    assert c4.adapter.challenge_count == (1 if case in {"other-session", "unknown"} else 0)


async def test_t011_c4_a_modified_plan_cannot_be_approved(c4: _C3Env) -> None:
    payload = await _c4_review(c4)
    c4.source.targets["plan-a"] = _c3_plan(c4.now, plan_ttl=3600, valid_ttl=3000)
    response = await _c4_decide(c4, payload)
    assert response.status_code == 409 and response.json() == _C4_CONFLICT
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))
    assert c4.adapter.challenge_count == 0


@pytest.mark.parametrize("case", ["missing", "raises", "acl", "tenant"])
async def test_t011_c4_reload_failures_are_generic_404_after_consuming(
    c4: _C3Env, case: str
) -> None:
    payload = await _c4_review(c4)
    if case == "missing":
        c4.source.targets.clear()
    elif case == "raises":
        c4.source.mode = "raises"
    elif case == "acl":
        c4.source.targets["plan-a"] = _c3_plan(c4.now, device="heater-a")
    else:
        c4.source.targets["plan-a"] = _c3_plan(c4.now, tenant="tenant-b")
    response = await _c4_decide(c4, payload)
    assert response.status_code == 404, response.text
    assert response.json() == {"error": "not_found", "message": "No se encontró el objetivo"}
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))
    assert c4.adapter.challenge_count == 0


async def test_t011_c4_rule_approval_is_unsupported_but_rule_rejection_works(
    c4: _C3Env,
) -> None:
    c4.source.targets["rule-a"] = _c3_rule(c4.now)
    reference = {"kind": "rule", "id": "rule-a"}
    first = await _c4_review(c4, reference)
    response = await _c4_decide(c4, first)
    assert response.status_code == 409 and response.json() == {
        "error": "decision_unsupported",
        "message": "Este tipo de revisión aún no se puede aprobar",
    }
    assert not c4.repository.nonce_exists_sync(_c4_nonce(first["challenge_id"]))
    assert c4.adapter.challenge_count == 0
    retry = await _c4_decide(c4, first)
    assert retry.status_code == 409 and retry.json() == _C4_CONFLICT
    second = await _c4_review(c4, reference)
    rejected = await _c4_decide(c4, second, action="reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"


async def test_t011_c4_close_during_reload_issues_nothing(c4: _C3Env) -> None:
    payload = await _c4_review(c4)

    async def close() -> None:
        c4.adapter.close()

    c4.source.hook = close
    response = await _c4_decide(c4, payload)
    assert response.status_code == 503, response.text
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))


async def test_t011_c4_session_expiry_during_reload_issues_nothing(c4: _C3Env) -> None:
    payload = await _c4_review(c4)

    async def expire() -> None:
        c4.files["wall"].set(c4.now + timedelta(seconds=601))
        c4.files["tick"][0] += 601

    c4.source.hook = expire
    response = await _c4_decide(c4, payload)
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))


@pytest.mark.parametrize(
    "error,status,body",
    [
        ("replayed", 409, {"error": "decision_conflict"}),
        ("required", 409, {"error": "decision_conflict"}),
        ("source", 503, {"error": "human_confirmation_unavailable"}),
        ("unavailable", 503, {"error": "human_confirmation_unavailable"}),
        ("sqlite", 500, {"error": "internal_error"}),
        ("boom", 500, {"error": "internal_error"}),
    ],
)
async def test_t011_c4_store_failures_map_to_fixed_bodies(
    c4: _C3Env, monkeypatch: pytest.MonkeyPatch, error: str, status: int, body: dict[str, str]
) -> None:
    from domoai.runtime.approval_store import ApprovalStore

    def failing(self: Any, *args: Any, **kwargs: Any) -> Any:
        if error == "boom":
            raise RuntimeError("secret store failure")
        if error == "sqlite":
            import sqlite3

            raise sqlite3.OperationalError("secret: database is locked")
        code = {
            "replayed": ErrorCode.APPROVAL_ASSERTION_REPLAYED,
            "required": ErrorCode.APPROVAL_REQUIRED,
            "source": ErrorCode.SOURCE_UNAVAILABLE,
            "unavailable": ErrorCode.HUMAN_CONFIRMATION_UNAVAILABLE,
        }[error]
        raise DomainError(code, "secret domain detail")

    monkeypatch.setattr(ApprovalStore, "issue_authenticated", failing)
    payload = await _c4_review(c4)
    response = await _c4_decide(c4, payload)
    assert response.status_code == status, response.text
    assert body.items() <= response.json().items()
    assert "secret" not in response.text and c4.adapter.challenge_count == 0


async def test_t011_c4_get_decisions_is_not_allowed(c4: _C3Env) -> None:
    assert (await c4.client.get("/api/v1/resident/decisions")).status_code == 405


async def test_t011_c4_shared_quota_applies_to_decisions(c4_store: dict[str, Any]) -> None:
    async with _c3_open(c4_store, rate_limit=4, approval_store=c4_store["store"]) as env:
        payload = await _c4_review(env)
        assert (await _c4_decide(env, payload, action="reject")).status_code == 200
        assert (await _c4_decide(env, payload, action="reject")).status_code == 409
        limited = await _c4_decide(env, payload, action="reject")
        assert limited.status_code == 429 and limited.json() == {"error": "rate_limited"}


async def test_t011_c4_close_clears_nothing_durable(c4: _C3Env) -> None:
    payload = await _c4_review(c4)
    approved = await _c4_decide(c4, payload)
    approval_id = approved.json()["approval_id"]
    c4.adapter.close()
    grant = c4.repository.get_sync(approval_id)
    assert grant is not None and grant.plan_id == "plan-a"


async def test_t011_c4_store_contract_smoke_validates_the_oracles(c4_store: dict[str, Any]) -> None:
    from domoai.runtime.approval_store import ApprovalAssertion, OperatorPrincipal

    now = c4_store["wall"].now()
    plan = _c3_plan(now)
    principal = OperatorPrincipal("resident", "resident_browser", "resident-session:smoke")
    assertion = ApprovalAssertion(
        principal,
        _c4_nonce("smoke"),
        now,
        now + timedelta(minutes=5),
        plan_id="plan-a",
        validation_digest="sha256:" + "1" * 64,
    )
    grant = c4_store["store"].issue_authenticated(plan, principal=principal, assertion=assertion)
    stored = c4_store["repository"].get_sync(grant.approval_id)
    assert stored is not None and stored.assertion_nonce == _c4_nonce("smoke")
    assert stored.expires_at == now + timedelta(minutes=5) and stored.approved_at == now
    assert c4_store["repository"].nonce_exists_sync(_c4_nonce("smoke"))


async def test_t011_c4_grant_expiry_follows_the_shorter_plan_lifetime(c4: _C3Env) -> None:
    c4.source.targets["plan-a"] = _c3_plan(c4.now, plan_ttl=200)
    payload = await _c4_review(c4)
    body = (await _c4_decide(c4, payload)).json()
    grant = c4.repository.get_sync(body["approval_id"])
    assert grant is not None and grant.expires_at == c4.now + timedelta(seconds=200)
    assert body["expires_at"] == _c3_z(c4.now + timedelta(seconds=200))


async def test_t011_c4_grant_times_come_from_the_decision_not_the_review(c4: _C3Env) -> None:
    payload = await _c4_review(c4)
    c4.files["wall"].set(c4.now + timedelta(seconds=60))
    c4.files["tick"][0] += 60
    body = (await _c4_decide(c4, payload)).json()
    grant = c4.repository.get_sync(body["approval_id"])
    assert grant is not None
    assert grant.approved_at == c4.now + timedelta(seconds=60)
    assert grant.expires_at == c4.now + timedelta(seconds=60) + timedelta(minutes=5)


async def test_t011_c4_reload_timeout_is_404_after_consuming(c4: _C3Env) -> None:
    payload = await _c4_review(c4)

    async def never() -> None:
        await anyio.sleep(30)

    c4.source.hook = never
    with anyio.fail_after(10):
        response = await _c4_decide(c4, payload)
    assert response.status_code == 404 and c4.adapter.challenge_count == 0
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))


async def test_t011_c4_projection_needing_clarification_is_422_after_consuming(
    c4: _C3Env,
) -> None:
    payload = await _c4_review(c4)
    c4.source.targets["plan-a"] = _c3_plan(c4.now, unit="degC")
    response = await _c4_decide(c4, payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "clarification_required"
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))
    assert c4.adapter.challenge_count == 0


async def test_t011_c4_credential_reload_during_reload_issues_nothing(c4: _C3Env) -> None:
    from domoai.mcp.token_lifecycle import TokenFileManager

    payload = await _c4_review(c4)

    async def rotate() -> None:
        TokenFileManager(c4.files["paths"]["resident"]).rotate(
            "resident-extra",
            scopes=["consent:human"],
            tenant_id="tenant-a",
            household_ids=["home-a"],
            roles=[PrincipalRole.OPERATOR],
            operations=["review_consent", "approve_consent"],
        )
        c4.files["verifiers"]["resident"].reload()

    c4.source.hook = rotate
    response = await _c4_decide(c4, payload)
    assert response.status_code == 503, response.text
    assert not c4.repository.nonce_exists_sync(_c4_nonce(payload["challenge_id"]))


# --- T011-C5 RED: retry/idempotency and GET /decisions/{request_id} (freeze p04-t011-c5 + R1) --

_C5_NOT_FOUND = {"error": "not_found", "message": "No se encontró el objetivo"}
_C5_DECISION_CONFLICT = {
    "error": "decision_conflict",
    "message": "No se pudo registrar la decisión; revisa de nuevo",
}


async def _c5_get(env: _C3Env, request_id: str, **kwargs: Any) -> Any:
    kwargs.setdefault("headers", env.headers(**{"Content-Type": None}))
    return await env.client.get(f"/api/v1/resident/decisions/{request_id}", **kwargs)


def _c5_count(env: _C3Env, table: str) -> int:
    connection = env.repository.database.connection
    value: int = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    connection.rollback()
    return value


async def _c5_approve(env: _C3Env, request_id: str = "req-1") -> tuple[dict[str, Any], Any]:
    payload = await _c4_review(env)
    response = await _c4_decide(env, payload, request_id=request_id)
    assert response.status_code == 200, response.text
    return payload, response


async def test_t011_c5_retry_after_a_lost_response_returns_the_same_bytes(c4: _C3Env) -> None:
    payload, first = await _c5_approve(c4)
    calls = len(c4.source.calls)
    retry = await _c4_decide(c4, payload)
    assert retry.status_code == 200 and retry.content == first.content
    _c3_secure(retry)
    assert len(c4.source.calls) == calls, "a retry must not reload the target"
    assert _c5_count(c4, "approval_grants") == 1 and _c5_count(c4, "consent_receipts") == 1
    assert c4.adapter.challenge_count == 0
    again = await _c4_decide(c4, payload)
    assert again.content == first.content and _c5_count(c4, "approval_grants") == 1


async def test_t011_c5_retry_from_another_session_of_the_same_identity_works(c4: _C3Env) -> None:
    payload, first = await _c5_approve(c4)
    other_session = await c4.new_session()
    retry = await _c4_decide(
        c4, payload, headers=c4.headers(**{"X-Resident-Session": other_session})
    )
    assert retry.status_code == 200 and retry.content == first.content


async def test_t011_c5_retry_survives_a_host_restart(c4_store: dict[str, Any]) -> None:
    store = c4_store["store"]
    async with _c3_open(c4_store, approval_store=store) as env:
        env.repository = c4_store["repository"]
        payload, first = await _c5_approve(env)
    c4_store["admission"] = _c1_admission(c4_store)
    async with _c3_open(c4_store, approval_store=store) as restarted:
        restarted.repository = c4_store["repository"]
        retry = await _c4_decide(restarted, payload)
        assert retry.status_code == 200 and retry.content == first.content
        assert (await _c5_get(restarted, "req-1")).content == first.content
        assert restarted.source.calls == []


@pytest.mark.parametrize("variant", ["reject", "digest", "challenge"])
async def test_t011_c5_same_request_id_with_other_content_is_a_conflict(
    c4: _C3Env, variant: str
) -> None:
    payload, first = await _c5_approve(c4)
    if variant == "reject":
        retry = await _c4_decide(c4, payload, action="reject")
    elif variant == "digest":
        retry = await _c4_decide(c4, payload, digest="sha256:" + "0" * 64)
    else:
        fresh = await _c4_review(c4)
        retry = await _c4_decide(c4, fresh)
    assert retry.status_code == 409 and retry.json() == _C5_DECISION_CONFLICT
    assert _c5_count(c4, "approval_grants") == 1
    assert c4.adapter.challenge_count == (1 if variant == "challenge" else 0)


async def test_t011_c5_another_identity_cannot_reuse_the_receipt(c4: _C3Env) -> None:
    payload, _ = await _c5_approve(c4)
    other_session = await c4.new_session("other")
    other = c4.headers(
        Authorization="Bearer " + c4.files["tokens"]["other"],
        **{"X-Resident-Session": other_session},
    )
    retry = await _c4_decide(c4, payload, headers=other)
    assert retry.status_code == 409 and retry.json() == _C4_CONFLICT
    assert _c5_count(c4, "approval_grants") == 1


async def test_t011_c5_concurrent_request_id_collision_issues_exactly_one_grant(
    c4: _C3Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.runtime.approval_store import ApprovalStore

    await _c5_approve(c4, "race")
    # Simulate the race: the pre-check misses, so only the SQL primary key can stop the second.
    monkeypatch.setattr(ApprovalStore, "get_decision_receipt", lambda self, *a, **k: None)
    fresh = await _c4_review(c4)
    second = await _c4_decide(c4, fresh, request_id="race")
    assert second.status_code == 409 and second.json() == _C5_DECISION_CONFLICT
    assert _c5_count(c4, "approval_grants") == 1
    assert _c5_count(c4, "consent_issuance_receipts") == 1


async def test_t011_c5_get_returns_the_stored_receipt_bytes(c4: _C3Env) -> None:
    _, first = await _c5_approve(c4)
    found = await _c5_get(c4, "req-1")
    assert found.status_code == 200 and found.content == first.content
    _c3_secure(found)
    head = await c4.client.head(
        "/api/v1/resident/decisions/req-1", headers=c4.headers(**{"Content-Type": None})
    )
    assert head.status_code == 200 and head.content == b""
    zero = await _c5_get(
        c4, "req-1", headers=c4.headers(**{"Content-Type": None, "Content-Length": "0"})
    )
    assert zero.status_code == 200


async def test_t011_c5_get_has_one_not_found_body_for_every_non_receipt(c4: _C3Env) -> None:
    from domoai.domain.models import AuthorityContext, PrincipalRole

    _, first = await _c5_approve(c4)
    approval_id = first.json()["approval_id"]
    c4.store.revoke_consent(
        approval_id,
        authority=AuthorityContext(
            tenant_id="tenant-a",
            household_id="home-a",
            household_ids=["home-a"],
            principal_id="resident",
            roles=[PrincipalRole.OPERATOR],
        ),
        request_id="rev-1",
    )
    other_session = await c4.new_session("other")
    other = c4.headers(
        **{"Content-Type": None},
        Authorization="Bearer " + c4.files["tokens"]["other"],
        **{"X-Resident-Session": other_session},
    )
    cases = [
        await _c5_get(c4, "unknown"),
        await _c5_get(c4, "req-1", headers=other),
        await _c5_get(c4, "rev-1", headers=other),
        await _c5_get(c4, "r" * 201),
    ]
    for response in cases:
        assert response.status_code == 404 and response.json() == _C5_NOT_FOUND
    assert len({response.content for response in cases}) == 1
    # C7: the identity's own revocation receipt is a readable decision receipt.
    own = await _c5_get(c4, "rev-1")
    assert own.status_code == 200 and own.json()["status"] == "revocation_pending"


@pytest.mark.parametrize(
    "changes,params,content,status",
    [
        ({"X-Resident-Session": None}, None, None, 401),
        ({"X-Resident-Session": "unknown"}, None, None, 401),
        ({"Authorization": "Bearer wrong"}, None, None, 401),
        ({"Origin": "https://attacker.example"}, None, None, 403),
        ({"Origin": None}, None, None, 403),
        ({}, {"x": "1"}, None, 400),
        ({}, None, b"12345", 400),
        ({"Transfer-Encoding": "chunked"}, None, None, 400),
    ],
)
async def test_t011_c5_get_admission_and_framing(
    c4: _C3Env, changes: dict[str, Any], params: Any, content: Any, status: int
) -> None:
    await _c5_approve(c4)
    response = await c4.client.request(
        "GET",
        "/api/v1/resident/decisions/req-1",
        headers=c4.headers(**{"Content-Type": None}, **changes),
        params=params,
        content=content,
    )
    assert response.status_code == status, response.text
    assert (response.headers.get("www-authenticate") == "Bearer") is (status == 401)


async def test_t011_c5_get_needs_the_approve_consent_operation(
    c2_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.runtime.approval_store import ApprovalStore

    monkeypatch.undo()
    async with _c3_open(c2_store, approval_store=ApprovalStore(clock=c2_store["wall"])) as env:
        response = await _c5_get(env, "req-1")
        assert response.status_code == 403, response.text


async def test_t011_c5_get_needs_an_approval_store(c4_store: dict[str, Any]) -> None:
    async with _c3_open(c4_store) as env:
        response = await _c5_get(env, "req-1")
        assert response.status_code == 503, response.text
        assert response.json()["error"] == "human_confirmation_unavailable"


async def test_t011_c5_unconfigured_host_registers_the_get_surface() -> None:
    app = create_management_app(StubManager())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://home.example"
    ) as client:
        response = await client.get("/api/v1/resident/decisions/req-1")
    assert response.status_code == 503
    assert response.json()["error"] == "human_confirmation_unavailable"


async def test_t011_c5_receipt_is_historical_after_revocation_and_expiry(c4: _C3Env) -> None:
    from domoai.domain.models import AuthorityContext, PrincipalRole

    payload, first = await _c5_approve(c4)
    c4.store.revoke_consent(
        first.json()["approval_id"],
        authority=AuthorityContext(
            tenant_id="tenant-a",
            household_id="home-a",
            household_ids=["home-a"],
            principal_id="resident",
            roles=[PrincipalRole.OPERATOR],
        ),
        request_id="rev-2",
    )
    c4.files["wall"].set(c4.now + timedelta(seconds=400))
    c4.files["tick"][0] += 400
    retry = await _c4_decide(c4, payload)
    assert retry.status_code == 200 and retry.content == first.content
    assert (await _c5_get(c4, "req-1")).content == first.content


async def test_t011_c5_without_persistence_a_retry_is_a_conflict(c4_store: dict[str, Any]) -> None:
    from domoai.runtime.approval_store import ApprovalStore

    memory = ApprovalStore(clock=c4_store["wall"])
    async with _c3_open(c4_store, approval_store=memory) as env:
        payload = await _c4_review(env)
        assert (await _c4_decide(env, payload)).status_code == 200
        retry = await _c4_decide(env, payload)
        assert retry.status_code == 409 and retry.json() == _C4_CONFLICT


async def test_t011_c5_receipt_row_holds_no_secret(c4: _C3Env) -> None:
    payload, _ = await _c5_approve(c4)
    connection = c4.repository.database.connection
    rows = connection.execute(
        "SELECT fingerprint, response FROM consent_receipts WHERE request_id='req-1'"
    ).fetchall()
    connection.rollback()
    assert len(rows) == 1, "the receipt row must exist before it can be checked for secrets"
    assert len(rows[0][0]) == 64 and set(rows[0][0]) <= set("0123456789abcdef")
    text = " ".join(str(value) for row in rows for value in row)
    for secret in (
        payload["challenge_id"],
        c4.session_id,
        c4.files["tokens"]["resident"],
        _c4_nonce(payload["challenge_id"]),
    ):
        assert secret not in text


async def test_t011_c5_reject_writes_no_receipt_and_a_later_approve_may_reuse_the_id(
    c4: _C3Env,
) -> None:
    payload = await _c4_review(c4)
    assert (await _c4_decide(c4, payload, action="reject")).status_code == 200
    assert _c5_count(c4, "consent_receipts") == 0
    fresh = await _c4_review(c4)
    approved = await _c4_decide(c4, fresh)
    assert approved.status_code == 200 and _c5_count(c4, "consent_receipts") == 1


async def test_t011_c5_get_checks_admission_before_framing(c4: _C3Env) -> None:
    await _c5_approve(c4)
    response = await c4.client.get(
        "/api/v1/resident/decisions/req-1",
        headers=c4.headers(**{"Content-Type": None, "X-Resident-Session": "unknown"}),
        params={"x": "1"},
    )
    assert response.status_code == 401


async def test_t011_c5_a_two_hundred_character_request_id_is_accepted_end_to_end(
    c4: _C3Env,
) -> None:
    request_id = "r" * 200
    _, first = await _c5_approve(c4, request_id)
    found = await _c5_get(c4, request_id)
    assert found.status_code == 200 and found.content == first.content


async def test_t011_c5_retry_needs_a_live_session(c4: _C3Env) -> None:
    payload, _ = await _c5_approve(c4)
    c4.files["wall"].set(c4.now + timedelta(seconds=601))
    c4.files["tick"][0] += 601
    retry = await _c4_decide(c4, payload)
    assert retry.status_code == 401 and retry.headers["www-authenticate"] == "Bearer"


# --- T011-C5b RED: hardening of the C5 review P2s ---------------------------------------------


async def test_t011_c5b_get_authorization_precedes_framing(
    c2_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.runtime.approval_store import ApprovalStore

    monkeypatch.undo()
    async with _c3_open(c2_store, approval_store=ApprovalStore(clock=c2_store["wall"])) as env:
        response = await env.client.get(
            "/api/v1/resident/decisions/req-1",
            headers=env.headers(**{"Content-Type": None}),
            params={"x": "1"},
        )
        assert response.status_code == 403, response.text


async def test_t011_c5b_receipt_bytes_do_not_depend_on_model_field_order(
    c4: _C3Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.domain.household_consent import ConsentControlVersion

    def reversed_dump(self: Any, **_: Any) -> dict[str, Any]:
        return {"revision": self.revision, "generation": str(self.generation)}

    monkeypatch.setattr(ConsentControlVersion, "model_dump", reversed_dump)
    payload, first = await _c5_approve(c4)
    retry = await _c4_decide(c4, payload)
    assert retry.status_code == 200 and retry.content == first.content
    assert (await _c5_get(c4, "req-1")).content == first.content


# --- T011-C7 RED: resident revocation (freeze p04-t011-c7-freeze.md + R1 + R2) ----------------

_C7_OPERATIONS = ["review_consent", "approve_consent", "revoke_consent"]


@asynccontextmanager
async def _c7_open(
    c1_files: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operations: list[str],
) -> Any:
    from domoai.mcp.token_lifecycle import TokenFileManager
    from domoai.persistence.repositories import ApprovalGrantRepository
    from domoai.persistence.sqlite import SQLiteDatabase
    from domoai.runtime.approval_store import ApprovalStore
    from domoai.runtime.clock import FixedClock

    monkeypatch.undo()
    _c1_change_record(c1_files, operations=operations)
    other = TokenFileManager(c1_files["paths"]["resident"]).rotate(
        "resident-other",
        scopes=["consent:human"],
        tenant_id="tenant-a",
        household_ids=["home-a"],
        roles=[PrincipalRole.OPERATOR],
        operations=operations,
    )
    c1_files["tokens"]["other"] = other.token
    c1_files["verifiers"]["resident"].reload()
    wall, tick = FixedClock(datetime.now(UTC)), [1000.0]
    database = SQLiteDatabase(tmp_path / "c7.sqlite3", clock=wall)
    await database.initialize()
    repository = ApprovalGrantRepository(database)
    store = ApprovalStore(clock=wall, persistence=repository)
    files = dict(
        c1_files,
        admission=_c1_admission(c1_files),
        wall=wall,
        tick=tick,
        store=store,
        repository=repository,
    )
    try:
        async with _c3_open(files, approval_store=store) as env:
            env.store, env.repository = store, repository
            yield env
    finally:
        await database.close()


@pytest.fixture
async def c7(c1_files: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    async with _c7_open(c1_files, tmp_path, monkeypatch, _C7_OPERATIONS) as env:
        yield env


_C7_REVOKE_KEYS = {"status", "approval_id", "revocation_id", "revoked_at", "control_version"}


async def _c7_revoke(
    env: _C3Env, request_id: str = "rev-1", reference: dict[str, Any] | None = None
) -> tuple[dict[str, Any], Any]:
    payload = await _c4_review(env, reference)
    response = await _c4_decide(env, payload, action="revoke", request_id=request_id)
    return payload, response


def _c7_head(env: _C3Env) -> Any:
    return env.repository.get_consent_control_sync(tenant_id="tenant-a", household_id="home-a")


async def test_t011_c7_revoke_returns_a_pending_receipt_and_blocks_the_grant(c7: _C3Env) -> None:
    from domoai.domain.errors import DomainError, ErrorCode

    _, approved = await _c5_approve(c7)
    approval_id = approved.json()["approval_id"]
    before = _c7_head(c7)
    _, response = await _c7_revoke(c7)
    assert response.status_code == 200, response.text
    _c3_secure(response)
    body = response.json()
    assert set(body) == _C7_REVOKE_KEYS
    assert body["status"] == "revocation_pending" and body["approval_id"] == approval_id
    assert body["revoked_at"].endswith("Z")
    after = _c7_head(c7)
    assert after.revision == before.revision + 1
    assert body["control_version"] == after.model_dump(mode="json")
    with pytest.raises(DomainError) as caught:
        c7.repository.assert_consent_active_sync(approval_id)
    assert caught.value.code == ErrorCode.CONSENT_REVOKED
    assert _c5_count(c7, "consent_revocations") == 1
    for secret in (c7.files["tokens"]["resident"], c7.session_id, "challenge", "review_digest"):
        assert secret not in response.text


async def test_t011_c7_retry_and_get_return_the_same_bytes_without_revoking_again(
    c7: _C3Env,
) -> None:
    await _c5_approve(c7)
    payload, first = await _c7_revoke(c7)
    calls = len(c7.source.calls)
    retry = await _c4_decide(c7, payload, action="revoke", request_id="rev-1")
    assert retry.status_code == 200 and retry.content == first.content
    assert len(c7.source.calls) == calls, "a retry must not reload the target"
    assert c7.adapter.challenge_count == 0
    other_session = await c7.new_session()
    again = await c7.client.post(
        "/api/v1/resident/decisions",
        headers=c7.headers(**{"X-Resident-Session": other_session}),
        content=json.dumps(
            {
                "challenge_id": payload["challenge_id"],
                "action": "revoke",
                "expected_review_digest": payload["review_digest"],
                "request_id": "rev-1",
            }
        ),
    )
    assert again.status_code == 200 and again.content == first.content
    got = await _c5_get(c7, "rev-1")
    assert got.status_code == 200 and got.content == first.content
    assert _c5_count(c7, "consent_revocations") == 1
    assert _c7_head(c7).revision == first.json()["control_version"]["revision"]


async def test_t011_c7_a_retry_with_another_body_never_returns_the_stored_receipt(
    c7: _C3Env,
) -> None:
    await _c5_approve(c7)
    await _c7_revoke(c7)
    second = await _c4_review(c7)
    response = await _c4_decide(c7, second, action="revoke", request_id="rev-1")
    assert response.status_code == 409, response.text
    assert response.json() == _C5_DECISION_CONFLICT
    assert _c5_count(c7, "consent_revocations") == 1


async def test_t011_c7_an_approval_request_id_cannot_be_reused_as_a_revocation(c7: _C3Env) -> None:
    await _c5_approve(c7, "req-1")
    payload = await _c4_review(c7)
    response = await _c4_decide(c7, payload, action="revoke", request_id="req-1")
    assert response.status_code == 409, response.text
    assert response.json() == _C5_DECISION_CONFLICT
    got = await _c5_get(c7, "req-1")
    assert got.status_code == 200 and got.json()["status"] == "approved"
    assert _c5_count(c7, "consent_revocations") == 0


async def test_t011_c7_without_an_active_approval_the_revoke_is_a_conflict(c7: _C3Env) -> None:
    _, response = await _c7_revoke(c7)
    assert response.status_code == 409, response.text
    assert response.json() == _C5_DECISION_CONFLICT
    assert _c5_count(c7, "consent_revocations") == 0


async def test_t011_c7_a_second_revocation_of_the_same_plan_is_a_conflict(c7: _C3Env) -> None:
    await _c5_approve(c7)
    await _c7_revoke(c7, "rev-1")
    head = _c7_head(c7)
    _, again = await _c7_revoke(c7, "rev-2")
    assert again.status_code == 409, again.text
    assert _c7_head(c7) == head
    assert _c5_count(c7, "consent_revocations") == 1


async def test_t011_c7_several_active_approvals_are_never_revoked_blindly(c7: _C3Env) -> None:
    await _c5_approve(c7, "req-1")
    await _c5_approve(c7, "req-2")
    _, response = await _c7_revoke(c7)
    assert response.status_code == 409, response.text
    assert response.json() == _C5_DECISION_CONFLICT
    assert _c5_count(c7, "consent_revocations") == 0


async def test_t011_c7_rules_are_not_revocable_and_nothing_is_listed(
    c7: _C3Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    c7.source.targets["rule-a"] = _c3_rule(c7.now)

    def exploding(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a rule must be rejected before any listing")

    monkeypatch.setattr(type(c7.store), "list_active_approvals", exploding, raising=False)
    _, response = await _c7_revoke(c7, reference={"kind": "rule", "id": "rule-a"})
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "decision_unsupported"
    assert _c5_count(c7, "consent_revocations") == 0


async def test_t011_c7_a_changed_plan_stops_the_revocation_and_the_challenge_is_spent(
    c7: _C3Env,
) -> None:
    await _c5_approve(c7)
    payload = await _c4_review(c7)
    c7.source.targets["plan-a"] = _c3_plan(c7.now, unit="degC")
    response = await _c4_decide(c7, payload, action="revoke", request_id="rev-1")
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "clarification_required"
    assert c7.adapter.challenge_count == 0
    assert _c5_count(c7, "consent_revocations") == 0


async def test_t011_c7_the_challenge_is_one_use_for_revocation(c7: _C3Env) -> None:
    await _c5_approve(c7)
    payload, first = await _c7_revoke(c7)
    assert first.status_code == 200, first.text
    replay = await _c4_decide(c7, payload, action="revoke", request_id="rev-9")
    assert replay.status_code == 409 and replay.json() == _C4_CONFLICT


async def test_t011_c7_revoke_needs_its_own_operation_and_does_not_consume_the_challenge(
    c1_files: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = ["review_consent", "approve_consent"]
    async with _c7_open(c1_files, tmp_path, monkeypatch, operations) as env:
        await _c5_approve(env)
        payload = await _c4_review(env)
        response = await _c4_decide(env, payload, action="revoke", request_id="rev-1")
        assert response.status_code == 403, response.text
        assert env.adapter.challenge_count == 1
        assert _c5_count(env, "consent_revocations") == 0


async def test_t011_c7_a_revoke_only_token_cannot_approve_but_reaches_the_revoke_path(
    c1_files: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = ["review_consent", "revoke_consent"]
    async with _c7_open(c1_files, tmp_path, monkeypatch, operations) as env:
        payload = await _c4_review(env)
        approve = await _c4_decide(env, payload, action="approve")
        assert approve.status_code == 403, approve.text
        assert env.adapter.challenge_count == 1
        reject = await _c4_decide(env, payload, action="reject", request_id="req-2")
        assert reject.status_code == 403, reject.text
        # Authorization passes for revoke: with nothing to revoke the answer is a conflict.
        revoke = await _c4_decide(env, payload, action="revoke", request_id="rev-1")
        assert revoke.status_code == 409 and revoke.json() == _C5_DECISION_CONFLICT


async def test_t011_c7_another_principal_cannot_read_a_revocation_receipt(c7: _C3Env) -> None:
    await _c5_approve(c7)
    await _c7_revoke(c7)
    other_session = await c7.new_session("other")
    response = await c7.client.get(
        "/api/v1/resident/decisions/rev-1",
        headers=c7.headers(
            Authorization="Bearer " + c7.files["tokens"]["other"],
            **{"X-Resident-Session": other_session, "Content-Type": None},
        ),
    )
    assert response.status_code == 404 and response.json() == _C5_NOT_FOUND


# --- T011-C7 RED (review R1): digest, ACL, approval choice and single-permission tokens -------


def _c7_seed_grant(env: _C3Env, nonce: str = "seed-nonce") -> Any:
    from domoai.runtime.approval_store import ApprovalAssertion, OperatorPrincipal

    plan = _c3_plan(env.now)
    principal = OperatorPrincipal("seeded-operator", "resident_browser", "seed-session")
    assertion = ApprovalAssertion(
        principal,
        nonce,
        env.now,
        env.now + timedelta(minutes=5),
        plan_id=plan.id,
        validation_digest=plan.validation.digest,
    )
    return env.store.issue_authenticated(plan, principal=principal, assertion=assertion)


def _c7_consume(env: _C3Env, grant: Any) -> None:
    assert env.repository.consume_if_pending_sync(grant.approval_id, now=env.now) is True


async def test_t011_c7_a_wrong_digest_never_revokes_and_spends_the_challenge(c7: _C3Env) -> None:
    _c7_seed_grant(c7)
    payload = await _c4_review(c7)
    response = await _c4_decide(c7, payload, action="revoke", digest=_C4_BAD_DIGEST)
    assert response.status_code == 409 and response.json() == _C4_CONFLICT
    assert c7.adapter.challenge_count == 0
    assert _c5_count(c7, "consent_revocations") == 0


async def test_t011_c7_a_modified_but_projectable_plan_cannot_be_revoked(c7: _C3Env) -> None:
    _c7_seed_grant(c7)
    payload = await _c4_review(c7)
    c7.source.targets["plan-a"] = _c3_plan(c7.now, plan_ttl=3600, valid_ttl=3000)
    response = await _c4_decide(c7, payload, action="revoke", request_id="rev-1")
    assert response.status_code == 409 and response.json() == _C4_CONFLICT
    assert c7.adapter.challenge_count == 0
    assert _c5_count(c7, "consent_revocations") == 0


@pytest.mark.parametrize("case", ["acl", "tenant", "missing"])
async def test_t011_c7_reload_failures_are_a_generic_404_and_never_revoke(
    c7: _C3Env, case: str
) -> None:
    _c7_seed_grant(c7)
    payload = await _c4_review(c7)
    if case == "acl":
        c7.source.targets["plan-a"] = _c3_plan(c7.now, device="heater-a")
    elif case == "tenant":
        c7.source.targets["plan-a"] = _c3_plan(c7.now, tenant="tenant-b")
    else:
        c7.source.targets.clear()
    response = await _c4_decide(c7, payload, action="revoke", request_id="rev-1")
    assert response.status_code == 404, response.text
    assert response.json() == {"error": "not_found", "message": "No se encontró el objetivo"}
    assert c7.adapter.challenge_count == 0
    assert _c5_count(c7, "consent_revocations") == 0


async def test_t011_c7_a_pending_approval_is_preferred_over_a_consumed_one(c7: _C3Env) -> None:
    consumed = _c7_seed_grant(c7, "n-consumed")
    _c7_consume(c7, consumed)
    pending = _c7_seed_grant(c7, "n-pending")
    _, response = await _c7_revoke(c7)
    assert response.status_code == 200, response.text
    assert response.json()["approval_id"] == pending.approval_id
    c7.repository.assert_consent_active_sync(consumed.approval_id)  # still active


async def test_t011_c7_a_single_consumed_approval_is_revoked_for_real(c7: _C3Env) -> None:
    from domoai.domain.errors import DomainError, ErrorCode

    consumed = _c7_seed_grant(c7)
    _c7_consume(c7, consumed)
    _, response = await _c7_revoke(c7)
    assert response.status_code == 200, response.text
    assert response.json()["approval_id"] == consumed.approval_id
    with pytest.raises(DomainError) as caught:
        c7.repository.assert_consent_active_sync(consumed.approval_id)
    assert caught.value.code == ErrorCode.CONSENT_REVOKED


async def test_t011_c7_two_consumed_approvals_are_ambiguous(c7: _C3Env) -> None:
    for nonce in ("n-1", "n-2"):
        _c7_consume(c7, _c7_seed_grant(c7, nonce))
    _, response = await _c7_revoke(c7)
    assert response.status_code == 409 and response.json() == _C5_DECISION_CONFLICT
    assert _c5_count(c7, "consent_revocations") == 0


async def test_t011_c7_an_expired_pending_approval_is_not_active_next_to_a_consumed_one(
    c7: _C3Env,
) -> None:
    consumed = _c7_seed_grant(c7, "n-consumed")
    _c7_consume(c7, consumed)
    _c7_seed_grant(c7, "n-expiring")
    c7.files["wall"].set(c7.now + timedelta(minutes=6))
    c7.files["tick"][0] += 360
    payload = await _c4_review(c7)
    response = await _c4_decide(c7, payload, action="revoke", request_id="rev-1")
    assert response.status_code == 200, response.text
    assert response.json()["approval_id"] == consumed.approval_id


async def test_t011_c7_a_revoke_only_token_revokes_and_reads_the_receipt(
    c1_files: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = ["review_consent", "revoke_consent"]
    async with _c7_open(c1_files, tmp_path, monkeypatch, operations) as env:
        grant = _c7_seed_grant(env)
        _, response = await _c7_revoke(env)
        assert response.status_code == 200, response.text
        assert response.json()["approval_id"] == grant.approval_id
        got = await _c5_get(env, "rev-1")
        assert got.status_code == 200 and got.content == response.content
        assert (await _c5_get(env, "missing")).status_code == 404


async def test_t011_c7_a_token_without_revoke_cannot_replay_a_revocation_receipt(
    c1_files: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = ["review_consent", "approve_consent"]
    async with _c7_open(c1_files, tmp_path, monkeypatch, operations) as env:
        payload = await _c4_review(env)
        retry = await _c4_decide(env, payload, action="revoke", request_id="rev-1")
        assert retry.status_code == 403, retry.text


# --- T011-C7 RED (source review P2): grants issued after the review are never revoked --------


async def test_t011_c7h_a_grant_issued_after_the_review_is_never_the_revocation_target(
    c7: _C3Env,
) -> None:
    seen = _c7_seed_grant(c7, "n-seen")
    _c7_consume(c7, seen)
    payload = await _c4_review(c7)
    late: list[Any] = []

    async def issue_late() -> None:
        c7.files["wall"].set(c7.now + timedelta(seconds=1))
        c7.files["tick"][0] += 1
        late.append(_c7_seed_grant(c7, "n-late"))

    c7.source.hook = issue_late
    response = await _c4_decide(c7, payload, action="revoke", request_id="rev-1")
    assert response.status_code == 200, response.text
    assert response.json()["approval_id"] == seen.approval_id
    c7.repository.assert_consent_active_sync(late[0].approval_id)  # never seen, never revoked


async def test_t011_c7h_a_store_failure_reading_a_receipt_is_a_503_not_an_unknown_id(
    c7: _C3Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.domain.errors import DomainError, ErrorCode

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise DomainError(ErrorCode.SOURCE_UNAVAILABLE, "store down")

    monkeypatch.setattr(type(c7.store), "get_consent_receipt", unavailable)
    response = await _c5_get(c7, "anything")
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "human_confirmation_unavailable"
