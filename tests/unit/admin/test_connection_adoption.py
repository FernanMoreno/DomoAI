from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from domoai.domain.coordination import LeaseScope
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.security.secrets import FileSecretStore
from tests.unit.persistence.test_connection_repository import _profile


@pytest.mark.asyncio
async def test_offline_adoption_is_reviewable_idempotent_and_reversible(tmp_path: Path) -> None:
    from domoai.admin.connection_adoption import (
        AdoptionEntry,
        AdoptionManifest,
        adopt_connections,
        profile_digest,
    )

    scope = LeaseScope(tenant_id="tenant", household_id="home-a", deployment_id="edge")
    legacy = FileSecretStore(tmp_path / "master.key")
    old_ref = legacy.put("preserved-secret")
    old_path = tmp_path / "secrets" / f"{old_ref[7:]}.json"
    old_bytes = old_path.read_bytes()
    new_ref = f"secret:{uuid4()}"
    profile = _profile().model_copy(update={"credential_ref": old_ref})
    database = SQLiteDatabase(tmp_path / "legacy.db")
    await database.initialize()
    repository = ConnectionRepository(database, scope=scope)
    await repository.save(profile)
    database.connection.execute("DELETE FROM connection_ownership")
    database.connection.commit()
    store = FileSecretStore(tmp_path / "master.key", scope=scope)
    manifest = AdoptionManifest(
        scope=scope,
        entries=(
            AdoptionEntry(
                connection_id=profile.id,
                expected_profile_digest=profile_digest(profile),
                source_credential_ref=old_ref,
                expected_ciphertext_digest=hashlib.sha256(old_bytes).hexdigest(),
                target_credential_ref=new_ref,
            ),
        ),
    )
    try:
        before = {
            str(p): p.read_bytes()
            for p in tmp_path.rglob("*")
            if p.is_file() and not p.name.endswith("-shm")
        }
        report = adopt_connections(database, manifest, store=store)
        assert report["changed"] == 0
        assert before == {
            str(p): p.read_bytes()
            for p in tmp_path.rglob("*")
            if p.is_file() and not p.name.endswith("-shm")
        }
        assert await repository.get(profile.id) is None
        assert adopt_connections(database, manifest, store=store, apply=True)["changed"] == 1
        adopted = await repository.get(profile.id)
        assert adopted is not None and adopted.credential_ref == new_ref
        assert store.resolve(new_ref) == "preserved-secret"
        assert old_path.read_bytes() == old_bytes
        assert adopt_connections(database, manifest, store=store, apply=True)["changed"] == 0
        assert (
            adopt_connections(database, manifest, store=store, apply=True, rollback=True)["changed"]
            == 1
        )
        assert await repository.get(profile.id) is None
        assert legacy.resolve(old_ref) == "preserved-secret"
        assert old_path.read_bytes() == old_bytes
        assert (
            adopt_connections(database, manifest, store=store, apply=True, rollback=True)["changed"]
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_adoption_rejects_stale_profile_without_binding(tmp_path: Path) -> None:
    from domoai.admin.connection_adoption import (
        AdoptionEntry,
        AdoptionManifest,
        ConnectionAdoptionError,
        adopt_connections,
        profile_digest,
    )

    scope = LeaseScope(tenant_id="tenant", household_id="home-a", deployment_id="edge")
    database = SQLiteDatabase(tmp_path / "legacy.db")
    await database.initialize()
    repository = ConnectionRepository(database, scope=scope)
    profile = _profile().model_copy(update={"credential_ref": None})
    await repository.save(profile)
    database.connection.execute("DELETE FROM connection_ownership")
    database.connection.commit()
    manifest = AdoptionManifest(
        scope=scope,
        entries=(
            AdoptionEntry(
                connection_id=profile.id,
                expected_profile_digest=profile_digest(profile),
            ),
        ),
    )
    try:
        adopt_connections(database, manifest)
        database.connection.execute(
            "UPDATE connections SET unique_id = ? WHERE id = ?", ("changed", str(profile.id))
        )
        database.connection.commit()
        with pytest.raises(ConnectionAdoptionError):
            adopt_connections(database, manifest, apply=True)
        assert await repository.get(profile.id) is None
    finally:
        await database.close()


def test_adoption_cli_defaults_to_review_without_applying() -> None:
    from domoai.admin.cli import _parser

    args = _parser().parse_args(
        ["adopt-connections", "--manifest", "review.json", "--database", "existing.db"]
    )
    assert args.apply is False
    assert args.rollback is False


def test_manifest_rejects_case_aliases_of_one_destination_uuid() -> None:
    from pydantic import ValidationError

    from domoai.admin.connection_adoption import AdoptionEntry, AdoptionManifest

    target = "abcdefab-abcd-abcd-abcd-abcdefabcdef"
    scope = LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge")
    with pytest.raises(ValidationError):
        AdoptionManifest(
            scope=scope,
            entries=tuple(
                AdoptionEntry(
                    connection_id=uuid4(),
                    expected_profile_digest="a" * 64,
                    source_credential_ref=f"secret:{uuid4()}",
                    expected_ciphertext_digest="b" * 64,
                    target_credential_ref=f"secret:{value}",
                )
                for value in (target, target.upper())
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_adoption_holds_secret_locks_until_database_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback: bool,
) -> None:
    import threading

    import domoai.admin.connection_adoption as adoption

    scope = LeaseScope(tenant_id="tenant", household_id="home-a", deployment_id="edge")
    legacy = FileSecretStore(tmp_path / "master.key")
    store = FileSecretStore(tmp_path / "master.key", scope=scope)
    old_ref, target_ref = legacy.put("secret-value"), f"secret:{uuid4()}"
    profile = _profile().model_copy(update={"credential_ref": old_ref})
    database = SQLiteDatabase(tmp_path / "legacy.db")
    await database.initialize()
    repository = ConnectionRepository(database, scope=scope)
    await repository.save(profile)
    database.connection.execute("DELETE FROM connection_ownership")
    database.connection.commit()
    manifest = adoption.AdoptionManifest(
        scope=scope,
        entries=(
            adoption.AdoptionEntry(
                connection_id=profile.id,
                expected_profile_digest=adoption.profile_digest(profile),
                source_credential_ref=old_ref,
                expected_ciphertext_digest=hashlib.sha256(
                    (tmp_path / "secrets" / f"{old_ref[7:]}.json").read_bytes()
                ).hexdigest(),
                target_credential_ref=target_ref,
            ),
        ),
    )
    if rollback:
        adoption.adopt_connections(database, manifest, store=store, apply=True)
    original = adoption._entry_state
    started, finished = threading.Event(), threading.Event()
    threads = []

    def delete() -> None:
        started.set()
        (legacy if rollback else store).delete(old_ref if rollback else target_ref)
        finished.set()

    def barrier(*args, **kwargs):
        if kwargs.get("lock"):
            thread = threading.Thread(target=delete)
            threads.append(thread)
            thread.start()
            assert started.wait(2)
            assert not finished.wait(0.2), "credential deleted before DB publication"
        return original(*args, **kwargs)

    monkeypatch.setattr(adoption, "_entry_state", barrier)
    try:
        adoption.adopt_connections(database, manifest, store=store, apply=True, rollback=rollback)
    finally:
        for thread in threads:
            thread.join(timeout=5)
        await database.close()
    assert finished.is_set()


@pytest.mark.asyncio
async def test_postgres_readonly_open_releases_connection_on_interruption() -> None:
    from domoai.persistence.postgres import PostgresDatabase

    class InterruptedConnection:
        closed = False

        def execute(self, *_args):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    connection = InterruptedConnection()
    database = PostgresDatabase("postgresql://fixture", connection_factory=lambda _: connection)
    with pytest.raises(KeyboardInterrupt):
        await database.open_existing()
    assert connection.closed


def test_adoption_cli_dry_run_cannot_create_missing_key_or_database(tmp_path: Path) -> None:
    import json

    from domoai.admin.cli import main

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "scope": {"tenant_id": "tenant", "household_id": "home", "deployment_id": "edge"},
                "entries": [{"connection_id": str(uuid4()), "expected_profile_digest": "a" * 64}],
            }
        )
    )
    key = tmp_path / "unprovisioned" / "master.key"
    database = tmp_path / "unprovisioned.db"
    assert (
        main(
            [
                "adopt-connections",
                "--manifest",
                str(manifest),
                "--database",
                str(database),
                "--master-key",
                str(key),
            ]
        )
        == 2
    )
    assert not key.parent.exists()
    assert not database.exists()


@pytest.mark.asyncio
async def test_adoption_database_failure_preserves_legacy_and_retry_reuses_copy(
    tmp_path: Path,
) -> None:
    import sqlite3

    from domoai.admin.connection_adoption import (
        AdoptionEntry,
        AdoptionManifest,
        ConnectionAdoptionError,
        adopt_connections,
        profile_digest,
    )

    scope = LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge")
    legacy = FileSecretStore(tmp_path / "master.key")
    old_ref, target_ref = legacy.put("preserve-me"), f"secret:{uuid4()}"
    store = FileSecretStore(tmp_path / "master.key", scope=scope)
    old_path = tmp_path / "secrets" / f"{old_ref[7:]}.json"
    original_bytes = old_path.read_bytes()
    profile = _profile().model_copy(update={"credential_ref": old_ref})
    database = SQLiteDatabase(tmp_path / "legacy.db")
    await database.initialize()
    repository = ConnectionRepository(database, scope=scope)
    await repository.save(profile)
    database.connection.execute("DELETE FROM connection_ownership")
    database.connection.execute(
        """CREATE TRIGGER reject_adoption BEFORE INSERT ON connection_ownership
        BEGIN SELECT RAISE(ABORT, 'fixture rejects binding'); END"""
    )
    database.connection.commit()
    manifest = AdoptionManifest(
        scope=scope,
        entries=(
            AdoptionEntry(
                connection_id=profile.id,
                expected_profile_digest=profile_digest(profile),
                source_credential_ref=old_ref,
                expected_ciphertext_digest=hashlib.sha256(original_bytes).hexdigest(),
                target_credential_ref=target_ref,
            ),
        ),
    )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            adopt_connections(database, manifest, store=store, apply=True)
        assert await repository.get(profile.id) is None
        assert (
            database.connection.execute(
                "SELECT credential_ref FROM connections WHERE id = ?", (str(profile.id),)
            ).fetchone()[0]
            == old_ref
        )
        assert old_path.read_bytes() == original_bytes
        staged = (tmp_path / "secrets" / f"{target_ref[7:]}.json").read_bytes()
        database.connection.execute("DROP TRIGGER reject_adoption")
        database.connection.commit()
        assert adopt_connections(database, manifest, store=store, apply=True)["changed"] == 1
        assert (tmp_path / "secrets" / f"{target_ref[7:]}.json").read_bytes() == staged
        # A later runtime mutation invalidates rollback's complete-profile CAS.
        await repository.set_enabled(profile.id, True)
        with pytest.raises(ConnectionAdoptionError, match="changed after review"):
            adopt_connections(database, manifest, store=store, apply=True, rollback=True)
        current = await repository.get(profile.id)
        assert current is not None and current.enabled and current.credential_ref == target_ref
        assert store.resolve(target_ref) == "preserve-me"
    finally:
        await database.close()
