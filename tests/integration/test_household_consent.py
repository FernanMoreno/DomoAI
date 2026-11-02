"""T004 scaffolding only: existing stores/adapters, no parallel consent protocol."""

from __future__ import annotations

import asyncio
import typing
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.domain.models import (
    AdapterExecutionAck,
    AuthorityContext,
    Command,
    Plan,
    PrincipalRole,
)
from domoai.persistence.repositories import ApprovalGrantRepository, PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore, OperatorPrincipal
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.household_onboarding import connection_databases

INITIAL_TIME = datetime(2026, 9, 13, 12, tzinfo=UTC)


@dataclass
class ControlledCheckpoint:
    """One-shot async pause; timeout is a hang guard, never an ordering oracle."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def pause(self) -> None:
        self.reached.set()
        await asyncio.wait_for(self.released.wait(), timeout=5)

    async def wait_until_reached(self) -> None:
        await asyncio.wait_for(self.reached.wait(), timeout=5)

    def release(self) -> None:
        self.released.set()


class ControlledHomeAdapter(SimulatedHomeAdapter):
    """Pause the existing simulation; deliberately does NOT claim receiver fencing."""

    def __init__(self, clock: FixedClock) -> None:
        super().__init__(clock=clock)
        self.before_write = ControlledCheckpoint()
        self.before_ack = ControlledCheckpoint()
        self.contexts: list[ExecutionContext | None] = []

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        await self.before_write.pause()
        result = await super().execute(command, execution_context)
        self.contexts.append(execution_context)
        await self.before_ack.pause()
        return result


@dataclass
class HouseholdFixture:
    resident: AuthorityContext
    resident_principal: OperatorPrincipal
    agent: AuthorityContext
    adapter: ControlledHomeAdapter
    registry: DeviceRegistry
    state_store: StateStore
    plans: PlanRepository

    def command(self) -> Command:
        light = next(device for device in self.registry.devices if device.type.value == "light")
        return Command(
            id=f"{self.agent.household_id}:command",
            device_id=light.id,
            command="turn_on",
            idempotency_key=f"{self.agent.household_id}:intent",
        )


@dataclass
class ConsentFixture:
    """Injection points for future product tests; resident metadata is not an assertion."""

    clock: FixedClock
    databases: tuple[SQLiteDatabase, SQLiteDatabase]
    stores: tuple[ApprovalStore, ApprovalStore]
    homes: tuple[HouseholdFixture, HouseholdFixture]


@asynccontextmanager
async def consent_fixture(tmp_path: Path, backend: str) -> AsyncIterator[ConsentFixture]:
    if backend not in {"sqlite", "postgres"}:
        raise ValueError("T004 backend must be sqlite or postgres")
    clock = FixedClock(INITIAL_TIME)
    async with AsyncExitStack() as cleanup:
        first, second = await cleanup.enter_async_context(connection_databases(tmp_path, backend))
        homes = []
        for suffix, database in zip(("a", "b"), (first, second), strict=True):
            household = f"p04-home-{suffix}"
            scope = {
                "tenant_id": "p04-tenant",
                "household_id": household,
                "household_ids": [household],
            }
            resident = AuthorityContext(
                **scope, principal_id=f"resident-{suffix}", roles=[PrincipalRole.OPERATOR]
            )
            agent = AuthorityContext(
                **scope, principal_id=f"agent-{suffix}", roles=[PrincipalRole.PLANNER]
            )
            adapter = ControlledHomeAdapter(clock)
            cleanup.push_async_callback(adapter.disconnect)
            await adapter.connect()
            registry = DeviceRegistry()
            state_store = StateStore(clock=clock)
            await DiscoveryService(
                adapter, registry, state_store, AuditLog(), clock=clock
            ).refresh()
            homes.append(
                HouseholdFixture(
                    resident=resident,
                    resident_principal=OperatorPrincipal(
                        id=resident.principal_id,
                        authentication_context="fixture-metadata-only",
                        session_id=f"resident-session-{suffix}",
                    ),
                    agent=agent,
                    adapter=adapter,
                    registry=registry,
                    state_store=state_store,
                    plans=PlanRepository(database, clock=clock),
                )
            )
        yield ConsentFixture(
            clock=clock,
            databases=(first, second),
            stores=(
                ApprovalStore(clock=clock, persistence=ApprovalGrantRepository(first)),
                ApprovalStore(clock=clock, persistence=ApprovalGrantRepository(second)),
            ),
            homes=(homes[0], homes[1]),
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t004_fixture_has_independent_connections_and_explicit_identities(
    tmp_path: Path, backend: str
) -> None:
    async with consent_fixture(tmp_path, backend) as fixture:
        first, second = fixture.databases
        home_a, home_b = fixture.homes
        assert first.connection is not second.connection
        assert fixture.stores[0] is not fixture.stores[1]
        for database in fixture.databases:
            assert database.connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
            database.connection.rollback()
        if backend == "postgres":
            assert (
                first.connection.execute("SELECT version()").fetchone()[0].startswith("PostgreSQL")
            )
        else:
            assert first.connection.execute("SELECT sqlite_version()").fetchone()[0]
        first.connection.rollback()
        assert home_a.resident.household_ids == ["p04-home-a"]
        assert home_b.resident.household_ids == ["p04-home-b"]
        assert (
            len(
                {
                    home_a.resident.principal_id,
                    home_a.agent.principal_id,
                    home_b.resident.principal_id,
                    home_b.agent.principal_id,
                }
            )
            == 4
        )
        assert home_a.resident_principal.session_id != home_b.resident_principal.session_id
        fixture.clock.set(INITIAL_TIME + timedelta(seconds=7))
        for home in fixture.homes:
            await home.plans.save(
                Plan(
                    id=home.agent.household_id,
                    created_at=fixture.clock.now(),
                    authority=home.agent,
                    commands=[home.command()],
                )
            )
        # Raw repositories deliberately read both homes: this proves fixture scope
        # survives shared persistence, not that P04 authority/ACL is implemented.
        for home in fixture.homes:
            observed = await PlanRepository(second, clock=fixture.clock).get(
                home.agent.household_id
            )
            assert observed is not None and observed.authority == home.agent
            assert observed.created_at == fixture.clock.now()
            assert home.adapter.calls == []
        timestamp = first.connection.execute(
            "SELECT updated_at FROM plans WHERE id = ?", (home_a.agent.household_id,)
        ).fetchone()[0]
        assert datetime.fromisoformat(str(timestamp)) == fixture.clock.now()


async def test_t012_a_active_snapshots_survive_disk_rotation_and_unlink(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier
    from domoai.mcp.token_lifecycle import TokenFileManager

    path = tmp_path / "tokens.json"
    manager = TokenFileManager(path)
    first = manager.rotate("resident", scopes=["consent:human"], roles=[PrincipalRole.OPERATOR])
    old = StaticBearerTokenVerifier.from_file(path)
    before = old.credential_snapshot()
    second = manager.rotate("resident", scopes=["consent:human"], roles=[PrincipalRole.OPERATOR])
    new = StaticBearerTokenVerifier.from_file(path)
    after = new.credential_snapshot()
    assert await old.verify_token(first.token) is not None
    assert await new.verify_token(first.token) is None
    assert await new.verify_token(second.token) is not None
    assert before.records[0].token_hash != after.records[0].token_hash
    assert before.revision_digest != after.revision_digest
    assert old.credential_snapshot() == before
    new.reload()  # no-op keeps revision when the loaded file identity is unchanged
    assert new.credential_snapshot() == after
    path.unlink()
    assert old.credential_snapshot() == before
    assert new.credential_snapshot() == after


def test_t012_a_reload_error_keeps_records_and_original_provenance(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier
    from domoai.mcp.token_lifecycle import TokenFileManager

    path = tmp_path / "tokens.json"
    TokenFileManager(path).rotate("resident")
    verifier = StaticBearerTokenVerifier.from_file(path)
    before = verifier.credential_snapshot()
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    invalid.replace(path)
    with pytest.raises(ValueError, match="invalid MCP client token file"):
        verifier.reload()
    assert verifier.credential_snapshot() == before
    path.unlink()
    with pytest.raises(ValueError, match="invalid MCP client token file"):
        verifier.reload()
    assert verifier.credential_snapshot() == before


def test_t012_a_source_is_the_open_descriptor_not_replaced_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from domoai.mcp.auth import StaticBearerTokenVerifier
    from domoai.mcp.token_lifecycle import TokenFileManager

    path = tmp_path / "tokens.json"
    replacement = tmp_path / "replacement.json"
    first = TokenFileManager(path).rotate("old")
    TokenFileManager(replacement).rotate("new")
    old_stat = path.stat()
    original_open = Path.open
    replaced = False

    def open_then_replace(opened_path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal replaced
        handle = original_open(opened_path, *args, **kwargs)
        if opened_path == path and not replaced:
            replaced = True
            replacement.replace(path)
        return handle

    monkeypatch.setattr(Path, "open", open_then_replace)
    verifier = StaticBearerTokenVerifier.from_file(path)
    assert replaced
    snapshot = verifier.credential_snapshot()
    assert snapshot.records[0].client_id == "old"
    assert snapshot.source is not None
    assert (snapshot.source.device_id, snapshot.source.inode) == (old_stat.st_dev, old_stat.st_ino)
    assert path.stat().st_ino != old_stat.st_ino
    assert first.token not in repr(snapshot)


def test_t012_a_alias_empty_file_and_unknown_direct_source(tmp_path: Path) -> None:
    import os

    from domoai.mcp.auth import ClientTokenRecord, StaticBearerTokenVerifier

    path = tmp_path / "tokens.json"
    path.write_text('{"clients": []}', encoding="utf-8")
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(path)
    hardlink = tmp_path / "hardlink.json"
    os.link(path, hardlink)
    original = StaticBearerTokenVerifier.from_file(path).credential_snapshot()
    symbolic = StaticBearerTokenVerifier.from_file(symlink).credential_snapshot()
    hard = StaticBearerTokenVerifier.from_file(hardlink).credential_snapshot()
    assert original.records == symbolic.records == hard.records == ()
    assert original.source is not None and symbolic.source is not None and hard.source is not None
    assert original.source == symbolic.source
    assert original.source.canonical_path != hard.source.canonical_path
    assert (original.source.device_id, original.source.inode) == (
        hard.source.device_id,
        hard.source.inode,
    )
    direct = StaticBearerTokenVerifier(
        (ClientTokenRecord(client_id="direct", token_hash="a" * 64),), path
    )
    assert direct.credential_snapshot().source is None
    assert len(direct.credential_snapshot().records) == 1
    with pytest.raises(ValueError, match="invalid MCP client token file"):
        StaticBearerTokenVerifier.from_file(tmp_path / "missing.json")


@pytest.fixture
def c2_http_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, typing.Any]:
    """Provision real isolated credential files; no database fixture or provider."""
    from domoai.application.authority import AuthorityPolicy
    from domoai.config.settings import Settings
    from domoai.mcp.auth import StaticBearerTokenVerifier
    from domoai.mcp.token_lifecycle import TokenFileManager

    def forbidden(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        raise AssertionError("session crossed target/approval/audit boundary")

    for owner, methods in (
        (PlanRepository, ("get", "save")),
        (ApprovalStore, ("__init__", "issue", "issue_assertion", "prepare_delivery")),
        (AuditLog, ("append",)),
    ):
        for method in methods:
            monkeypatch.setattr(owner, method, forbidden)

    files: dict[str, Path] = {}
    tokens: dict[str, str] = {}
    verifiers: dict[str, StaticBearerTokenVerifier] = {}
    for name in ("resident", "mcp", "management"):
        path = tmp_path / (name + ".json")
        files[name] = path
        tokens[name] = (
            TokenFileManager(path)
            .rotate(
                name,
                scopes=["consent:human"] if name == "resident" else ["manage"],
                tenant_id="tenant-a",
                household_ids=["home-a"],
                roles=[PrincipalRole.OWNER],
                operations=["review_consent"] if name == "resident" else ["manage_connections"],
            )
            .token
        )
        verifiers[name] = StaticBearerTokenVerifier.from_file(path)
    return dict(
        paths=files,
        tokens=tokens,
        verifiers=verifiers,
        settings=Settings(
            resident_token_file=files["resident"], resident_origin="https://home.example"
        ),
        policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
    )


def _c2_http_adapter(files: dict[str, typing.Any]) -> typing.Any:
    import domoai.management.consent as module

    admission = module.ResidentAdmission(
        settings=files["settings"],
        resident_verifier=files["verifiers"]["resident"],
        peers=module.ResidentPeerInventory(
            frozenset({"mcp", "management", "management-static"}),
            (
                module.InspectableCredentialPeer("mcp", files["verifiers"]["mcp"]),
                module.InspectableCredentialPeer("management", files["verifiers"]["management"]),
                module.AbsentCredentialPeer("management-static"),
            ),
        ),
        authority_policy=files["policy"],
    )
    return module.ResidentConsentAdapter(admission=admission)


class _C2ForbiddenManager:
    def __getattribute__(self, name: str) -> typing.Any:
        raise AssertionError("session crossed manager boundary: " + name)


def _c2_headers(files: dict[str, typing.Any]) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + files["tokens"]["resident"],
        "Origin": "https://home.example",
    }


def _c2_secure(response: typing.Any) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    if response.status_code != 401:
        assert "www-authenticate" not in response.headers
    assert not {
        "set-cookie",
        "access-control-allow-origin",
        "location",
        "retry-after",
    }.intersection(response.headers)


async def test_t011_c_session_http_positive_lifespan_and_restart(
    c2_http_files: dict[str, typing.Any],
) -> None:
    import httpx

    import domoai.management.consent as module
    from domoai.management.api import create_management_app

    adapter = _c2_http_adapter(c2_http_files)
    app = create_management_app(_C2ForbiddenManager(), resident_consent=adapter)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://home.example"
        ) as client:
            response = await client.post(
                "/api/v1/resident/session", headers=_c2_headers(c2_http_files)
            )
            assert response.status_code == 201
            _c2_secure(response)
            assert "www-authenticate" not in response.headers
            payload = response.json()
            assert set(payload) == {"session_id", "expires_at"}
            assert payload["expires_at"].endswith("Z")
            session = await adapter.authenticate_session(
                payload["session_id"],
                "Bearer " + c2_http_files["tokens"]["resident"],
                origin="https://home.example",
                host="home.example",
            )
            assert session.identity.principal_id == "resident"
            assert adapter.session_count == 1
            with pytest.raises(ValueError, match="already belongs to an app"):
                create_management_app(_C2ForbiddenManager(), resident_consent=adapter)
            assert adapter.session_count == 1
    assert adapter.session_count == 0
    with pytest.raises(module.ResidentAdmissionError):
        adapter.ensure_available()
    fresh = _c2_http_adapter(c2_http_files)
    new_app = create_management_app(_C2ForbiddenManager(), resident_consent=fresh)
    async with new_app.router.lifespan_context(new_app):
        with pytest.raises(module.ResidentSessionError) as caught:
            await fresh.authenticate_session(
                payload["session_id"],
                "Bearer " + c2_http_files["tokens"]["resident"],
                origin="https://home.example",
                host="home.example",
            )
        assert caught.value.status_code == 401
        assert fresh.session_count == 0


def test_t011_c_session_http_rejects_nonconcrete_injection() -> None:
    from domoai.management.api import create_management_app

    invalid: typing.Any = object()
    with pytest.raises(ValueError, match="resident_consent must be a ResidentConsentAdapter"):
        create_management_app(_C2ForbiddenManager(), resident_consent=invalid)


@pytest.mark.parametrize(
    "case,status",
    [
        ("good", 201),
        ("no-origin", 403),
        ("cross", 403),
        ("cross-body", 403),
        ("duplicate-origin", 403),
        ("duplicate-host", 403),
        ("duplicate-auth", 401),
        ("invalid-token", 401),
        ("mcp-token", 401),
        ("management-token", 401),
        ("body", 400),
        ("json", 400),
        ("body-bad-token", 400),
        ("query", 400),
        ("large", 413),
        ("bad-length", 400),
        ("negative-length", 400),
        ("long-zero-length", 400),
        ("long-small-length", 400),
        ("duplicate-length", 400),
        ("transfer", 400),
        ("closed-body", 503),
        ("closed-origin", 503),
    ],
)
async def test_t011_c_session_http_precedence(
    c2_http_files: dict[str, typing.Any], case: str, status: int
) -> None:
    import httpx

    from domoai.management.api import create_management_app

    adapter = _c2_http_adapter(c2_http_files)
    calls: list[str] = []

    async def forbidden_verifier(token: str) -> typing.Any:
        calls.append(token)
        raise AssertionError("session called management verifier")

    app = create_management_app(
        _C2ForbiddenManager(),
        resident_consent=adapter,
        max_request_body_size=16,
        auth_verifier=forbidden_verifier,
        authority_policy=c2_http_files["policy"],
    )
    headers = list(_c2_headers(c2_http_files).items())
    body, path = b"", "/api/v1/resident/session"
    if case == "no-origin":
        headers = headers[:1]
    if case in {"cross", "cross-body"}:
        headers[1] = ("Origin", "https://evil.example")
    if case == "duplicate-origin":
        headers.append(headers[1])
    if case == "duplicate-host":
        headers.extend([("Host", "home.example"), ("Host", "evil.example")])
    if case == "duplicate-auth":
        headers.append(headers[0])
    if case in {"invalid-token", "body-bad-token"}:
        headers[0] = ("Authorization", "Bearer wrong")
    if case in {"mcp-token", "management-token"}:
        headers[0] = (
            "Authorization",
            "Bearer " + c2_http_files["tokens"][case.removesuffix("-token")],
        )
    if case in {"body", "body-bad-token", "closed-body", "cross-body"}:
        body = b"x"
    if case == "json":
        body = b"{}"
    if case == "large":
        body = b"x" * 17
    if case == "query":
        path += "?actor=owner"
    if case == "bad-length":
        headers.append(("Content-Length", "bogus"))
    if case == "negative-length":
        headers.append(("Content-Length", "-1"))
    if case == "long-zero-length":
        headers.append(("Content-Length", "0" * 5000))
    if case == "long-small-length":
        headers.append(("Content-Length", "0" * 5000 + "1"))
    if case == "duplicate-length":
        headers.extend([("Content-Length", "0"), ("Content-Length", "0")])
    if case == "transfer":
        headers.append(("Transfer-Encoding", "chunked"))
    if case.startswith("closed"):
        adapter.close()
        headers[1] = ("Origin", "https://evil.example")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://home.example"
    ) as client:
        response = await client.post(path, headers=headers, content=body)
    assert response.status_code == status
    _c2_secure(response)
    assert calls == []
    assert adapter.session_count == (1 if status == 201 else 0)
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {
            "error": "operator_authentication_failed",
            "message": "La credencial del residente no es válida",
        }
    elif status == 403:
        assert response.json() == {
            "error": "insufficient_scope",
            "message": "La solicitud del residente no está autorizada",
        }
    elif status == 400:
        assert response.json() == {
            "error": "validation_error",
            "message": "La solicitud de sesión del residente debe tener el cuerpo vacío",
        }
    elif status == 413:
        assert response.json() == {
            "error": "payload_too_large",
            "message": "request body exceeds configured limit",
        }
    elif status == 503:
        assert response.json() == {
            "error": "human_confirmation_unavailable",
            "message": "No está disponible la confirmación del residente",
        }


async def test_t011_c_session_http_quota_shared_once(c2_http_files: dict[str, typing.Any]) -> None:
    import httpx

    from domoai.management.api import create_management_app

    adapter = _c2_http_adapter(c2_http_files)
    app = create_management_app(_C2ForbiddenManager(), resident_consent=adapter, rate_limit=2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://home.example"
    ) as client:
        assert (
            await client.post("/api/v1/resident/session", headers=_c2_headers(c2_http_files))
        ).status_code == 201
        assert (await client.post("/api/v1/resident/decisions")).status_code == 403
        response = await client.post(
            "/api/v1/resident/session", content=b"bad", headers={"Origin": "evil"}
        )
        assert response.status_code == 429 and response.json() == {"error": "rate_limited"}
        _c2_secure(response)
    assert adapter.session_count == 1


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("empty", 201),
        ("byte", 400),
        ("large", 413),
        ("timeout", 408),
        ("close", 503),
        ("close-byte", 503),
        ("close-timeout", 503),
        ("quota", 429),
        ("none", 503),
    ],
)
async def test_t011_c_session_http_controlled_stream(
    c2_http_files: dict[str, typing.Any], mode: str, expected: int
) -> None:
    import anyio
    import httpx

    from domoai.management.api import create_management_app

    adapter = _c2_http_adapter(c2_http_files)
    app = create_management_app(
        _C2ForbiddenManager(),
        resident_consent=None if mode == "none" else adapter,
        rate_limit=1,
        max_request_body_size=16,
    )
    if mode == "quota":
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://home.example"
        ) as client:
            assert (await client.post("/api/v1/resident/decisions")).status_code == 403
    entered, release, done = anyio.Event(), anyio.Event(), anyio.Event()
    progress = anyio.Event()
    messages: list[typing.Any] = []
    receives = 0

    async def receive() -> typing.Any:
        nonlocal receives
        receives += 1
        if receives > 1:
            await anyio.sleep_forever()
        entered.set()
        progress.set()
        await release.wait()
        return {
            "type": "http.request",
            "body": b"x" * (17 if mode == "large" else 1)
            if mode in {"byte", "large", "close-byte"}
            else b"",
            "more_body": False,
        }

    async def send(message: typing.Any) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            done.set()
            progress.set()

    scope = dict(
        type="http",
        asgi={"version": "3.0", "spec_version": "2.3"},
        http_version="1.1",
        method="POST",
        scheme="https",
        path="/api/v1/resident/session",
        raw_path=b"/api/v1/resident/session",
        query_string=b"",
        root_path="",
        client=("127.0.0.1", 1),
        server=("home.example", 443),
        headers=[
            (b"host", b"home.example"),
            (b"origin", b"https://home.example"),
            (b"authorization", ("Bearer " + c2_http_files["tokens"]["resident"]).encode()),
            (b"content-length", b"0"),
        ],
    )
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(app, scope, receive, send)
            try:
                if mode not in {"quota", "none"}:
                    await progress.wait()
                    assert adapter.session_count == 0 and not done.is_set()
                    assert entered.is_set()
                    if mode.startswith("close"):
                        adapter.close()
                    if mode not in {"timeout", "close-timeout"}:
                        release.set()
                await done.wait()
            finally:
                group.cancel_scope.cancel()
                release.set()
    start = next(message for message in messages if message["type"] == "http.response.start")
    response = httpx.Response(
        start["status"],
        headers=start["headers"],
        content=b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        ),
    )
    assert response.status_code == expected
    _c2_secure(response)
    assert adapter.session_count == (1 if expected == 201 else 0)
    if expected == 408:
        assert response.json() == {
            "error": "validation_error",
            "message": "La solicitud de sesión del residente no se completó a tiempo",
        }


@pytest.mark.parametrize("exit_kind", ["error", "cancel"])
async def test_t011_c_session_http_lifespan_abnormal_cleanup(
    c2_http_files: dict[str, typing.Any], exit_kind: str
) -> None:
    import anyio

    from domoai.management.api import create_management_app

    adapter = _c2_http_adapter(c2_http_files)
    app = create_management_app(_C2ForbiddenManager(), resident_consent=adapter)
    try:
        with anyio.CancelScope() as scope:
            async with app.router.lifespan_context(app):
                await adapter.create_session(
                    "Bearer " + c2_http_files["tokens"]["resident"],
                    origin="https://home.example",
                    host="home.example",
                )
                if exit_kind == "error":
                    raise RuntimeError("test lifecycle failure")
                scope.cancel()
                await anyio.lowlevel.checkpoint()
    except RuntimeError as error:
        assert str(error) == "test lifecycle failure"
    assert adapter.session_count == 0


_RESIDENT_UNAVAILABLE = {
    "error": "human_confirmation_unavailable",
    "message": "No está disponible la confirmación del residente",
}


@asynccontextmanager
async def _served_run_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, resident_pair: bool
) -> AsyncIterator[str]:
    """Run the production entrypoint (env Settings, real Uvicorn); never a test-built app."""
    import socket

    from domoai.mcp.gateway import run_gateway

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        "DOMOAI_DATABASE_PATH": str(tmp_path / "runtime.sqlite3"),
        "DOMOAI_AUDIT_DATABASE_PATH": str(tmp_path / "audit.sqlite3"),
        "DOMOAI_CONNECTION_MASTER_KEY_PATH": str(tmp_path / "master.key"),
        "DOMOAI_MCP_HOST": "127.0.0.1",
        "DOMOAI_MCP_PORT": str(port),
        "DOMOAI_MCP_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "DOMOAI_MCP_PATH": "/mcp",
    }
    if resident_pair:
        env["DOMOAI_RESIDENT_TOKEN_FILE"] = str(tmp_path / "resident-not-provisioned.json")
        env["DOMOAI_RESIDENT_ORIGIN"] = "http://127.0.0.1:" + str(port)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    task = asyncio.create_task(run_gateway())
    try:
        for _ in range(200):
            if task.done():
                await task
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            await writer.wait_closed()
            break
        else:
            raise AssertionError("run_gateway did not start listening")
        yield f"http://127.0.0.1:{port}"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("resident_pair", [False, True])
async def test_t012_b0_served_app_rejects_resident_posts_with_adapter_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resident_pair: bool
) -> None:
    import httpx

    async with _served_run_gateway(tmp_path, monkeypatch, resident_pair=resident_pair) as base:
        async with httpx.AsyncClient(base_url=base) as client:
            health = await client.get("/healthz")
            assert health.status_code == 200
            for path in (
                "/api/v1/resident/session",
                "/api/v1/resident/reviews",
                "/api/v1/resident/decisions",
                "/api/v1/resident/%73ession",
            ):
                response = await client.post(path, headers={"Origin": "http://evil.example"})
                assert response.status_code == 503, path
                assert response.json() == _RESIDENT_UNAVAILABLE
                _c2_secure(response)
            for method, path in (
                ("GET", "/api/v1/resident/session"),
                ("HEAD", "/api/v1/resident/session"),
                ("OPTIONS", "/api/v1/resident/session"),
                ("POST", "/api/v1/resident/session/"),
                ("POST", "/api/v1/resident/sessions"),
                ("POST", "/mcp"),
            ):
                response = await client.request(method, path)
                if path.endswith("/session/"):
                    assert response.status_code == 307, (method, path)
                else:
                    assert 400 <= response.status_code < 500, (method, path, response.status_code)
                if method not in {"HEAD", "OPTIONS"}:
                    assert "human_confirmation_unavailable" not in response.text, (method, path)
            sse = await client.get("/mcp", headers={"Accept": "text/event-stream"})
            assert sse.status_code == 405


async def test_t012_b0_dispatcher_is_installed_in_the_composed_gateway_app(tmp_path: Path) -> None:
    from domoai.mcp.gateway import _ResidentDispatchMiddleware
    from tests.fixtures.household_onboarding import onboarding_gateway

    async with onboarding_gateway(tmp_path) as gateway:
        classes = [middleware.cls for middleware in gateway.app.user_middleware]
        assert _ResidentDispatchMiddleware in classes


def _b1_provision(tmp_path: Path, *, shared_resident_file: bool = False) -> dict[str, typing.Any]:
    """Two real offline-provisioned credential files; no fake runtime or provider."""
    from domoai.mcp.token_lifecycle import TokenFileManager

    paths = {"mcp": tmp_path / "mcp.json", "resident": tmp_path / "resident.json"}
    if shared_resident_file:
        paths["resident"] = paths["mcp"]
    tokens: dict[str, str] = {}
    for name, scopes, ops in (
        ("mcp", ["manage"], ["manage_connections"]),
        ("resident", ["consent:human"], ["review_consent"]),
    ):
        tokens[name] = (
            TokenFileManager(paths[name])
            .rotate(
                name,
                scopes=scopes,
                tenant_id="tenant-a",
                household_ids=["home-a"],
                roles=[PrincipalRole.OWNER],
                operations=ops,
            )
            .token
        )
    return {"paths": paths, "tokens": tokens}


def _b1_overrides(files: dict[str, typing.Any]) -> dict[str, typing.Any]:
    return dict(
        mcp_client_token_file=files["paths"]["mcp"],
        resident_token_file=files["paths"]["resident"],
        resident_origin="http://127.0.0.1:8765",
    )


async def test_t012_b1_session_gateway_opens_only_for_the_resident_credential(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        async with gateway.http_client() as client:
            origin = {"Origin": "http://127.0.0.1:8765"}
            opened = await client.post(
                "/api/v1/resident/session",
                headers={**origin, "Authorization": "Bearer " + files["tokens"]["resident"]},
            )
            assert opened.status_code == 201, opened.text
            mcp_bearer = await client.post(
                "/api/v1/resident/session",
                headers={**origin, "Authorization": "Bearer " + files["tokens"]["mcp"]},
            )
            assert mcp_bearer.status_code == 401
            resident_on_mcp = await client.post(
                "/mcp", headers={"Authorization": "Bearer " + files["tokens"]["resident"]}
            )
            assert resident_on_mcp.status_code == 401
            resident_on_management = await client.get(
                "/api/v1/connections",
                headers={"Authorization": "Bearer " + files["tokens"]["resident"]},
            )
            assert resident_on_management.status_code in {401, 403}


async def test_t012_b1_inventory_shared_source_keeps_resident_closed(tmp_path: Path) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path, shared_resident_file=True)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        async with gateway.http_client() as client:
            response = await client.post(
                "/api/v1/resident/session",
                headers={
                    "Origin": "http://127.0.0.1:8765",
                    "Authorization": "Bearer " + files["tokens"]["resident"],
                },
            )
            assert response.status_code == 503
            assert response.json() == _RESIDENT_UNAVAILABLE


async def test_t012_b1_each_credential_file_is_loaded_once_by_the_built_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    loaded: list[Path] = []
    original = StaticBearerTokenVerifier.from_file.__func__

    def counting(cls: typing.Any, path: typing.Any, *args: typing.Any, **kwargs: typing.Any):
        loaded.append(Path(path))
        return original(cls, path, *args, **kwargs)

    monkeypatch.setattr(StaticBearerTokenVerifier, "from_file", classmethod(counting))
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)):
        pass
    assert loaded.count(files["paths"]["mcp"]) == 1
    assert loaded.count(files["paths"]["resident"]) == 1


async def test_t012_b1_failed_construction_releases_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import domoai.management.api as api
    from tests.fixtures.household_onboarding import onboarding_gateway

    def exploding(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        raise RuntimeError("management construction failed")

    with monkeypatch.context() as patch:
        patch.setattr(api, "create_management_app", exploding)
        with pytest.raises(RuntimeError, match="management construction failed"):
            async with onboarding_gateway(tmp_path):
                pass
    async with onboarding_gateway(tmp_path) as gateway:
        assert gateway.runtime.ownership is not None


async def test_t012_b1_missing_resident_file_keeps_mcp_alive_and_resident_closed(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    overrides = _b1_overrides(files) | {"resident_token_file": tmp_path / "absent.json"}
    async with onboarding_gateway(tmp_path, **overrides) as gateway:
        async with gateway.http_client() as client:
            closed = await client.post(
                "/api/v1/resident/session",
                headers={"Origin": "http://127.0.0.1:8765"},
            )
            assert closed.status_code == 503
            assert closed.json() == _RESIDENT_UNAVAILABLE
            mcp = await client.post("/mcp")
            assert mcp.status_code == 401


async def test_t012_b1_absent_pair_performs_no_resident_file_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    loaded: list[Path] = []
    original = StaticBearerTokenVerifier.from_file.__func__

    def counting(cls: typing.Any, path: typing.Any, *args: typing.Any, **kwargs: typing.Any):
        loaded.append(Path(path))
        return original(cls, path, *args, **kwargs)

    monkeypatch.setattr(StaticBearerTokenVerifier, "from_file", classmethod(counting))
    async with onboarding_gateway(tmp_path, mcp_client_token_file=files["paths"]["mcp"]):
        pass
    assert loaded == [files["paths"]["mcp"]]


async def test_t012_b1_without_mcp_file_the_resident_host_still_opens(tmp_path: Path) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    overrides = _b1_overrides(files)
    del overrides["mcp_client_token_file"]
    async with onboarding_gateway(tmp_path, **overrides) as gateway:
        async with gateway.http_client() as client:
            opened = await client.post(
                "/api/v1/resident/session",
                headers={
                    "Origin": "http://127.0.0.1:8765",
                    "Authorization": "Bearer " + files["tokens"]["resident"],
                },
            )
            assert opened.status_code == 201, opened.text


_B2_ORIGIN = {"Origin": "http://127.0.0.1:8765"}


def _b2_resident_headers(files: dict[str, typing.Any]) -> dict[str, str]:
    return {**_B2_ORIGIN, "Authorization": "Bearer " + files["tokens"]["resident"]}


async def test_t012_b2_reload_closes_resident_even_when_files_are_unchanged(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        async with gateway.http_client() as client:
            first = await client.post(
                "/api/v1/resident/session", headers=_b2_resident_headers(files)
            )
            assert first.status_code == 201
            gateway.reload_credentials()
            again = await client.post(
                "/api/v1/resident/session", headers=_b2_resident_headers(files)
            )
            assert again.status_code == 503
            assert again.json() == _RESIDENT_UNAVAILABLE
        async with gateway.http_client() as client:
            later = await client.post(
                "/api/v1/resident/session", headers=_b2_resident_headers(files)
            )
            assert later.status_code == 503


async def test_t012_b2_failed_reload_raises_sanitized_and_stays_closed(tmp_path: Path) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        files["paths"]["resident"].write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError) as caught:
            gateway.reload_credentials()
        assert str(files["paths"]["resident"]) not in str(caught.value)
        assert files["tokens"]["resident"] not in str(caught.value)
        async with gateway.http_client() as client:
            response = await client.post(
                "/api/v1/resident/session", headers=_b2_resident_headers(files)
            )
            assert response.status_code == 503


async def test_t012_b2_restart_reopens_only_with_a_new_gateway_and_revoked_bearer_fails(
    tmp_path: Path,
) -> None:
    from domoai.mcp.token_lifecycle import TokenFileManager
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        async with gateway.http_client() as client:
            old = await client.post("/api/v1/resident/session", headers=_b2_resident_headers(files))
            assert old.status_code == 201
        gateway.reload_credentials()
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as fresh:
        async with fresh.http_client() as client:
            new = await client.post("/api/v1/resident/session", headers=_b2_resident_headers(files))
            assert new.status_code == 201
            assert new.json()["session_id"] != old.json()["session_id"]
    assert TokenFileManager(files["paths"]["resident"]).revoke("resident") is True
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as revoked:
        async with revoked.http_client() as client:
            denied = await client.post(
                "/api/v1/resident/session", headers=_b2_resident_headers(files)
            )
            assert denied.status_code == 401


async def test_t012_b2_close_closes_resident_first_and_blocks_later_reload(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        await gateway.close()
        async with gateway.http_client() as client:
            response = await client.post(
                "/api/v1/resident/session", headers=_b2_resident_headers(files)
            )
            assert response.status_code == 503
        with pytest.raises(ValueError):
            gateway.reload_credentials()


async def test_t012_b2_reload_wins_over_an_in_flight_resident_verification(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        verifier = gateway._resident_verifier
        reached, release = asyncio.Event(), asyncio.Event()
        real = verifier.verify_token

        async def held(token: str) -> typing.Any:
            reached.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return await real(token)

        verifier.verify_token = held
        async with gateway.http_client() as client:
            pending = asyncio.create_task(
                client.post("/api/v1/resident/session", headers=_b2_resident_headers(files))
            )
            await asyncio.wait_for(reached.wait(), timeout=5)
            gateway.reload_credentials()
            release.set()
            response = await pending
            assert response.status_code == 503


async def test_t012_b2_reload_closes_c2_before_any_loader_io_and_reloads_each_once(
    tmp_path: Path,
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        adapter = gateway._resident_consent
        async with gateway.http_client() as client:
            opened = await client.post(
                "/api/v1/resident/session", headers=_b2_resident_headers(files)
            )
            assert opened.status_code == 201
        assert adapter.session_count == 1
        calls: list[tuple[str, int]] = []
        for name, verifier in (
            ("mcp", gateway.client_token_verifier),
            ("resident", gateway._resident_verifier),
        ):
            real = verifier.reload

            def spy(name: str = name, real: typing.Any = real) -> None:
                calls.append((name, adapter.session_count))
                real()

            verifier.reload = spy
        gateway.reload_credentials()
        assert calls == [("mcp", 0), ("resident", 0)]


async def test_t012_b2_close_runs_runtime_cleanup_when_session_termination_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        released: list[bool] = []
        real_close = gateway.runtime.close

        async def tracked_close() -> None:
            released.append(True)
            await real_close()

        async def failing_terminate() -> None:
            raise RuntimeError("transport termination failed")

        with monkeypatch.context() as patch:
            patch.setattr(gateway.runtime, "close", tracked_close)
            patch.setattr(gateway, "close_http_sessions", failing_terminate)
            with pytest.raises(RuntimeError, match="transport termination failed"):
                await gateway.close()
        assert released == [True]
        assert gateway._resident_consent.session_count == 0


async def test_t012_b2_lifespan_exit_closes_the_resident_host(tmp_path: Path) -> None:
    from tests.fixtures.household_onboarding import onboarding_gateway

    files = _b1_provision(tmp_path)
    async with onboarding_gateway(tmp_path, **_b1_overrides(files)) as gateway:
        async with gateway.app.router.lifespan_context(gateway.app):
            gateway._resident_consent.ensure_available()
        with pytest.raises(Exception, match="confirmación del residente"):
            gateway._resident_consent.ensure_available()
