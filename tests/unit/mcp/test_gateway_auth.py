import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP

from domoai.domain.errors import DomainError, ErrorCode
from domoai.mcp.domotics_server import _authorize_mutation
from domoai.mcp.request_context import with_request_principal
from domoai.runtime.events import AuditLog
from domoai.runtime.execution_context import current_execution_principal


def test_valid_client_token_resolves_to_server_owned_identity(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"clients": [{
        "client_id": "codex",
        "token_hash": hashlib.sha256(b"codex-secret").hexdigest(),
        "scopes": ["home:read", "home:plan"],
    }]}))
    verifier = StaticBearerTokenVerifier.from_file(token_file)

    result = asyncio.run(verifier.verify_token("codex-secret"))

    assert isinstance(result, AccessToken)
    assert result.client_id == "codex"
    assert result.scopes == ["home:read", "home:plan"]


def test_invalid_client_token_is_rejected_without_echoing_secret(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"clients": [{
        "client_id": "claude",
        "token_hash": hashlib.sha256(b"claude-secret").hexdigest(),
        "scopes": ["home:read"],
    }]}))
    verifier = StaticBearerTokenVerifier.from_file(token_file)

    result = asyncio.run(verifier.verify_token("wrong-secret"))

    assert result is None
    assert "claude-secret" not in repr(verifier)


def test_client_token_file_rejects_non_hex_sha256_hash(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {"clients": [{"client_id": "bad", "token_hash": "g" * 64}]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid MCP client token file"):
        StaticBearerTokenVerifier.from_file(token_file)


def test_client_token_file_rejects_duplicate_client_ids_and_hashes(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_hash = hashlib.sha256(b"shared-secret").hexdigest()
    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "clients": [
                    {"client_id": "one", "token_hash": token_hash},
                    {"client_id": "two", "token_hash": token_hash},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid MCP client token file"):
        StaticBearerTokenVerifier.from_file(token_file)


def test_client_token_file_rejects_naive_expiry(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "naive",
                        "token_hash": hashlib.sha256(b"naive-secret").hexdigest(),
                        "expires_at": "2030-01-01T00:00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid MCP client token file"):
        StaticBearerTokenVerifier.from_file(token_file)


def test_client_token_verifier_reload_revokes_removed_token(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_hash = hashlib.sha256(b"reload-secret").hexdigest()
    token_file.write_text(
        json.dumps({"clients": [{"client_id": "reload", "token_hash": token_hash}]}),
        encoding="utf-8",
    )
    verifier = StaticBearerTokenVerifier.from_file(token_file)
    assert asyncio.run(verifier.verify_token("reload-secret")) is not None

    token_file.write_text(json.dumps({"clients": []}), encoding="utf-8")
    verifier.reload()

    assert asyncio.run(verifier.verify_token("reload-secret")) is None


def test_client_scope_is_required_for_physical_mutations() -> None:
    from domoai.mcp.auth import require_client_scope

    read_only = AccessToken(token="opaque", client_id="codex", scopes=["read"])

    with pytest.raises(DomainError) as excinfo:
        require_client_scope(read_only, "mutate")

    assert excinfo.value.code is ErrorCode.INSUFFICIENT_SCOPE
    require_client_scope(AccessToken(token="opaque", client_id="codex", scopes=["*"]), "mutate")


def test_expired_and_disabled_client_tokens_are_rejected(tmp_path: Path) -> None:
    from domoai.mcp.auth import StaticBearerTokenVerifier

    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "expired",
                        "token_hash": hashlib.sha256(b"expired-secret").hexdigest(),
                        "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    },
                    {
                        "client_id": "disabled",
                        "token_hash": hashlib.sha256(b"disabled-secret").hexdigest(),
                        "enabled": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    verifier = StaticBearerTokenVerifier.from_file(token_file)

    assert asyncio.run(verifier.verify_token("expired-secret")) is None
    assert asyncio.run(verifier.verify_token("disabled-secret")) is None


@pytest.mark.asyncio
async def test_authenticated_tool_identity_is_scoped_without_leaking_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp.server.auth.middleware.auth_context as auth_context

    server = FastMCP("identity-test")
    seen: list[str] = []

    @server.tool()
    @with_request_principal
    async def observe_identity() -> str:
        seen.append(current_execution_principal())
        return current_execution_principal()

    monkeypatch.setattr(
        auth_context,
        "get_access_token",
        lambda: AccessToken(token="opaque", client_id="codex", scopes=["read"]),
    )

    result = await server.call_tool("observe_identity", {})

    assert result[1]["result"] == "codex"
    assert seen == ["codex"]
    assert current_execution_principal() == "local"


def test_denied_mutation_is_audited_without_persisting_the_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import mcp.server.auth.middleware.auth_context as auth_context

    audit = AuditLog()
    context = SimpleNamespace(
        facade=SimpleNamespace(plan_service=SimpleNamespace(audit=audit))
    )
    secret = "read-only-secret"
    monkeypatch.setattr(
        auth_context,
        "get_access_token",
        lambda: AccessToken(token=secret, client_id="claude", scopes=["read"]),
    )

    with pytest.raises(DomainError) as excinfo:
        _authorize_mutation(context, operation="execute_plan", subject_id="plan-1")

    assert excinfo.value.code is ErrorCode.INSUFFICIENT_SCOPE
    event = audit.events[-1]
    assert event.event_type == "mcp_authorization_rejected"
    assert event.actor == "agent:claude"
    assert secret not in event.model_dump_json()


def test_t012_a_snapshot_shape_digest_and_deep_immutability(tmp_path: Path) -> None:
    from dataclasses import FrozenInstanceError

    from domoai.domain.models import PrincipalRole
    from domoai.mcp.auth import StaticBearerTokenVerifier

    record = {
        "client_id": "resident",
        "token_hash": hashlib.sha256(b"snapshot-secret").hexdigest(),
        "scopes": ["consent:human", "consent:human"],
        "enabled": True,
        "created_at": "2026-01-01T01:00:00+01:00",
        "expires_at": "2030-01-01T00:00:00+00:00",
        "revoked_at": None,
        "tenant_id": "tenant-a",
        "household_ids": ["home-a", "home-b"],
        "roles": ["operator"],
        "management_authority_explicit": True,
        "area_ids": ["room"],
        "device_ids": ["light"],
        "capabilities": ["switch"],
        "operations": ["review_consent"],
    }
    path = tmp_path / "records.json"
    path.write_text(json.dumps({"clients": [record]}), encoding="utf-8")
    verifier = StaticBearerTokenVerifier.from_file(path)
    snapshot = verifier.credential_snapshot()
    assert snapshot.schema_version == "domoai.credential-snapshot.v1"
    assert snapshot.source is not None
    stat = path.stat()
    source = {"canonical_path": str(path.resolve()), "device_id": stat.st_dev, "inode": stat.st_ino}
    assert snapshot.source.canonical_path == source["canonical_path"]
    assert (snapshot.source.device_id, snapshot.source.inode) == (stat.st_dev, stat.st_ino)
    item = snapshot.records[0]
    assert item.roles == (PrincipalRole.OPERATOR,)
    assert item.scopes == ("consent:human", "consent:human")
    assert item.operations == ("review_consent",)
    assert item.area_ids == ("room",) and item.device_ids == ("light",)
    assert item.capabilities == ("switch",) and item.household_ids == ("home-a", "home-b")
    assert item.management_authority_explicit is True
    assert item.provided_fields == frozenset(record)
    expected_record = dict(record)
    expected_record.update(
        created_at="2026-01-01T00:00:00.000000+00:00",
        expires_at="2030-01-01T00:00:00.000000+00:00",
        provided_fields=sorted(record),
    )
    payload = {
        "schema_version": "domoai.credential-snapshot.v1",
        "source": source,
        "records": [expected_record],
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    ).hexdigest()
    assert snapshot.revision_digest == expected_digest
    assert snapshot == verifier.credential_snapshot()
    for obj, attr in [
        (snapshot, "records"),
        (snapshot.source, "canonical_path"),
        (item, "tenant_id"),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, "changed")
    for values in (
        item.roles,
        item.scopes,
        item.operations,
        item.area_ids,
        item.device_ids,
        item.capabilities,
        item.household_ids,
        snapshot.records,
    ):
        assert isinstance(values, tuple)
        with pytest.raises(AttributeError):
            getattr(values, "append")("changed")  # noqa: B009 -- deliberate invalid mutation
    assert isinstance(item.provided_fields, frozenset)
    assert "snapshot-secret" not in repr(snapshot) + repr(item)
    assert item.token_hash not in repr(snapshot) + repr(item)
    access = asyncio.run(verifier.verify_token("snapshot-secret"))
    assert access is not None and access.claims is not None
    assert access.claims["authority"]["tenant_id"] == "tenant-a"


def test_t012_a_snapshot_preserves_legacy_marker_and_inactive_records(tmp_path: Path) -> None:
    from domoai.mcp.auth import ClientTokenDocument, StaticBearerTokenVerifier

    changes: list[dict[str, object]] = [
        {},
        {"enabled": False},
        {"revoked_at": "2026-01-01T00:00:00Z"},
        {"expires_at": "2020-01-01T00:00:00Z"},
    ]
    records = [
        {
            "client_id": str(i),
            "token_hash": hashlib.sha256(str(i).encode()).hexdigest(),
            "scopes": ["manage"],
            **change,
        }
        for i, change in enumerate(changes)
    ]
    path = tmp_path / "records.json"
    path.write_text(json.dumps({"clients": records}), encoding="utf-8")
    verifier = StaticBearerTokenVerifier.from_file(path)
    snapshot = verifier.credential_snapshot()
    assert len(snapshot.records) == 4
    assert all(
        item.roles == () and item.management_authority_explicit is False
        for item in snapshot.records
    )
    assert snapshot.records[0].provided_fields == frozenset(
        {"client_id", "token_hash", "scopes", "management_authority_explicit"}
    )
    # Same serialization contract as TokenFileManager: defaults do not promote intent.
    document = ClientTokenDocument.model_validate_json(path.read_text())
    path.write_text(document.model_dump_json(), encoding="utf-8")
    verifier.reload()
    assert all(
        item.management_authority_explicit is False
        for item in verifier.credential_snapshot().records
    )
    assert asyncio.run(verifier.verify_token("0")) is not None  # legacy unchanged


@pytest.mark.parametrize(
    "changes",
    [
        {"scopes": [[]]},
        {"roles": ["invented"]},
        {"operations": None},
        {"token_hash": "invalid"},
        {"expires_at": datetime(2030, 1, 1)},
        {"management_authority_explicit": "true"},
    ],
)
def test_t012_a_snapshot_rejects_bypassed_active_record(changes: dict[str, object]) -> None:
    from domoai.mcp.auth import ClientTokenRecord, StaticBearerTokenVerifier

    record = ClientTokenRecord(client_id="resident", token_hash="a" * 64).model_copy(update=changes)
    verifier = StaticBearerTokenVerifier((record,))
    with pytest.raises(ValueError, match="invalid active credential snapshot"):
        verifier.credential_snapshot()


def test_t012_a_incomplete_record_cannot_publish_partial_snapshot() -> None:
    from domoai.mcp.auth import ClientTokenRecord, StaticBearerTokenVerifier

    verifier = StaticBearerTokenVerifier((ClientTokenRecord.model_construct(token_hash="a" * 64),))
    with pytest.raises(ValueError, match="invalid active credential snapshot"):
        verifier.credential_snapshot()


@pytest.mark.parametrize(
    "field_name,year", [("created_at", 2020), ("expires_at", 2099), ("revoked_at", 2021)]
)
@pytest.mark.parametrize("offset_hours", [0, 2])
def test_t012_a_r1_snapshot_detaches_mutable_datetime(
    field_name: str, year: int, offset_hours: int
) -> None:
    from datetime import timedelta, timezone

    from domoai.mcp.auth import ClientTokenRecord, StaticBearerTokenVerifier

    class MutableDateTime(datetime):
        expired = False

        def timestamp(self) -> float:
            return 0.0 if self.expired else super().timestamp()

    zone = timezone(timedelta(hours=offset_hours))
    value = MutableDateTime(year, 1, 2, 3, 4, 5, 123456, tzinfo=zone)
    record = ClientTokenRecord.model_validate(
        {
            "client_id": "resident",
            "token_hash": hashlib.sha256(b"datetime-secret").hexdigest(),
            field_name: value,
        }
    )
    verifier = StaticBearerTokenVerifier((record,))
    admitted = asyncio.run(verifier.verify_token("datetime-secret")) is not None
    assert admitted is (field_name != "revoked_at")
    snapshot = verifier.credential_snapshot()
    exported = getattr(snapshot.records[0], field_name)
    expected = datetime(year, 1, 2, 3 - offset_hours, 4, 5, 123456, tzinfo=UTC)
    assert type(exported) is datetime
    assert exported == expected and exported.tzinfo is UTC
    assert exported is not getattr(record, field_name)
    with pytest.raises(AttributeError):
        setattr(exported, "expired", True)  # noqa: B010 -- deliberate invalid mutation
    assert value.expired is False
    assert (asyncio.run(verifier.verify_token("datetime-secret")) is not None) is admitted
    value.expired = True  # Mutation of active input cannot alter the detached view either.
    assert exported == expected and exported.timestamp() == expected.timestamp()


def test_t012_a_r1_reload_snapshot_never_pairs_new_records_with_old_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident

    from domoai.mcp.auth import StaticBearerTokenVerifier
    from domoai.mcp.token_lifecycle import TokenFileManager

    path = tmp_path / "records.json"
    manager = TokenFileManager(path)
    first = manager.rotate("resident", scopes=["consent:human"])
    verifier = StaticBearerTokenVerifier.from_file(path)
    before = verifier.credential_snapshot()
    second = manager.rotate("resident", scopes=["consent:human"])
    expected_source = path.stat()
    expected = StaticBearerTokenVerifier.from_file(path).credential_snapshot()
    assert before.source is not None and expected.source is not None
    assert before.source.inode != expected_source.st_ino
    assert before.records[0].token_hash == hashlib.sha256(first.token.encode()).hexdigest()

    records_published, release_writer, reader_attempted = Event(), Event(), Event()
    reader_id: int | None = None
    reader_blocked = False
    real_lock = verifier._snapshot_lock

    class ObservedLock:
        """Observe a real acquisition so the scheduler needs no sleep/negative timeout."""

        def __enter__(self) -> None:
            nonlocal reader_blocked
            if get_ident() == reader_id:
                acquired = real_lock.acquire(blocking=False)
                reader_blocked = not acquired
                reader_attempted.set()
                if not acquired:
                    assert real_lock.acquire(timeout=5), "reader lock acquisition timed out"
            else:
                assert real_lock.acquire(timeout=5), "writer lock acquisition timed out"

        def __exit__(self, *args: object) -> None:
            real_lock.release()

    def pause_publication(instance: object, name: str, value: object) -> None:
        object.__setattr__(instance, name, value)
        if instance is verifier and name == "_records":
            records_published.set()
            assert release_writer.wait(5), "publication barrier timed out"

    def read_snapshot() -> object:
        nonlocal reader_id
        reader_id = get_ident()
        return verifier.credential_snapshot()

    monkeypatch.setattr(verifier, "_snapshot_lock", ObservedLock())
    monkeypatch.setattr(StaticBearerTokenVerifier, "__setattr__", pause_publication)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(verifier.reload)
        try:
            assert records_published.wait(5), "reload did not reach publication"
            reader = pool.submit(read_snapshot)
            assert reader_attempted.wait(5), "snapshot did not reach acquisition"
            if reader_blocked:
                release_writer.set()
            observed = reader.result(timeout=5)
        finally:
            release_writer.set()
            writer.result(timeout=5)
    # With publication lock removed the reader finishes while source is still old.
    assert observed == expected, "snapshot mixed new records with old source/revision"
    assert verifier.credential_snapshot() == expected
    assert expected.records[0].token_hash == hashlib.sha256(second.token.encode()).hexdigest()
    assert expected.source.inode == expected_source.st_ino
