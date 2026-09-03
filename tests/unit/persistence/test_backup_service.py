import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import domoai.persistence.backup as backup_module
from domoai.persistence.backup import BackupError, BackupService, BackupSource
from domoai.persistence.sqlite import SQLiteDatabase


async def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    await database.initialize()
    return database


def _insert_marker(database: SQLiteDatabase, table: str, value: str) -> None:
    database.connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (value TEXT NOT NULL)")
    database.connection.execute(f"INSERT INTO {table} (value) VALUES (?)", (value,))
    database.connection.commit()


@pytest.mark.asyncio
async def test_sqlite_online_backup_captures_data_and_migration_ledger(tmp_path: Path) -> None:
    source = await _database(tmp_path / "source.sqlite3")
    _insert_marker(source, "backup_probe", "from-wal-backed-source")
    destination = tmp_path / "backup.sqlite3"

    result = source.backup_to(destination)

    assert destination.is_file()
    assert "001_initial.sql" in result.schema_migrations
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("SELECT value FROM backup_probe").fetchone() == (
            "from-wal-backed-source",
        )
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        restored.close()
        await source.close()


@pytest.mark.asyncio
async def test_create_publishes_operational_and_audit_members_with_redacted_manifest(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "data"
    operational = await _database(source_dir / "operational.sqlite3")
    audit = await _database(source_dir / "audit.sqlite3")
    _insert_marker(operational, "plans_probe", "plan-1")
    _insert_marker(audit, "audit_probe", "event-1")
    service = BackupService()

    manifest = await service.create(
        sources=(
            BackupSource("operational", operational),
            BackupSource("audit", audit),
        ),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )

    backup_dir = tmp_path / "backups" / manifest.backup_id
    assert backup_dir.is_dir()
    assert (backup_dir / "domoai.sqlite3").is_file()
    assert (backup_dir / "domoai-audit.sqlite3").is_file()
    assert (backup_dir / "manifest.json").is_file()
    assert (backup_dir / "COMPLETE").is_file()
    assert {member.name for member in manifest.members} == {"operational", "audit"}
    raw_manifest = (backup_dir / "manifest.json").read_text(encoding="utf-8")
    assert str(source_dir) not in raw_manifest
    assert str(operational.path) not in raw_manifest
    assert str(audit.path) not in raw_manifest
    assert "home-lab" in raw_manifest
    assert "token" not in raw_manifest.lower()

    verified = service.verify(backup_dir)
    assert verified.backup_id == manifest.backup_id
    assert {member.name for member in verified.members} == {"operational", "audit"}
    await operational.close()
    await audit.close()


@pytest.mark.asyncio
async def test_verify_rejects_member_digest_change_without_target_access(tmp_path: Path) -> None:
    source_dir = tmp_path / "data"
    operational = await _database(source_dir / "operational.sqlite3")
    audit = await _database(source_dir / "audit.sqlite3")
    service = BackupService()
    manifest = await service.create(
        sources=(
            BackupSource("operational", operational),
            BackupSource("audit", audit),
        ),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    member_path = tmp_path / "backups" / manifest.backup_id / "domoai.sqlite3"
    with member_path.open("ab") as handle:
        handle.write(b"corruption")

    with pytest.raises(BackupError) as error:
        service.verify(member_path.parent)

    assert error.value.code == "backup_digest_mismatch"
    await operational.close()
    await audit.close()


@pytest.mark.asyncio
async def test_repeated_create_keeps_previous_published_backup_readable(tmp_path: Path) -> None:
    source_dir = tmp_path / "data"
    operational = await _database(source_dir / "operational.sqlite3")
    audit = await _database(source_dir / "audit.sqlite3")
    service = BackupService()
    sources = (BackupSource("operational", operational), BackupSource("audit", audit))

    first = await service.create(
        sources=sources, output_dir=tmp_path / "backups", deployment_id="home-lab"
    )
    second = await service.create(
        sources=sources, output_dir=tmp_path / "backups", deployment_id="home-lab"
    )

    assert first.backup_id != second.backup_id
    assert service.verify(tmp_path / "backups" / first.backup_id).backup_id == first.backup_id
    assert service.verify(tmp_path / "backups" / second.backup_id).backup_id == second.backup_id
    await operational.close()
    await audit.close()


@pytest.mark.asyncio
async def test_verify_rejects_missing_completion_marker(tmp_path: Path) -> None:
    source_dir = tmp_path / "data"
    operational = await _database(source_dir / "operational.sqlite3")
    audit = await _database(source_dir / "audit.sqlite3")
    service = BackupService()
    manifest = await service.create(
        sources=(BackupSource("operational", operational), BackupSource("audit", audit)),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    (tmp_path / "backups" / manifest.backup_id / "COMPLETE").unlink()

    with pytest.raises(BackupError) as error:
        service.verify(tmp_path / "backups" / manifest.backup_id)

    assert error.value.code == "backup_manifest_invalid"
    await operational.close()
    await audit.close()


@pytest.mark.asyncio
async def test_verify_rejects_manifest_path_traversal_and_missing_completion_marker(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "data"
    operational = await _database(source_dir / "operational.sqlite3")
    audit = await _database(source_dir / "audit.sqlite3")
    service = BackupService()
    manifest = await service.create(
        sources=(
            BackupSource("operational", operational),
            BackupSource("audit", audit),
        ),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    backup_dir = tmp_path / "backups" / manifest.backup_id
    payload = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    payload["members"][0]["filename"] = "../outside.sqlite3"
    (backup_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BackupError) as error:
        service.verify(backup_dir)
    assert error.value.code == "backup_manifest_invalid"

    await operational.close()
    await audit.close()


@pytest.mark.asyncio
async def test_verify_rejects_swapped_member_mapping_even_with_valid_manifest_digest(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "data"
    operational = await _database(source_dir / "operational.sqlite3")
    audit = await _database(source_dir / "audit.sqlite3")
    service = BackupService()
    manifest = await service.create(
        sources=(BackupSource("operational", operational), BackupSource("audit", audit)),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    manifest_path = tmp_path / "backups" / manifest.backup_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    first, second = payload["members"]
    first["filename"], second["filename"] = second["filename"], first["filename"]
    unsigned = dict(payload)
    unsigned.pop("manifest_sha256")
    payload["manifest_sha256"] = hashlib.sha256(
        (json.dumps(unsigned, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BackupError) as error:
        service.verify(manifest_path.parent)

    assert error.value.code == "backup_manifest_invalid"
    await operational.close()
    await audit.close()


@pytest.mark.asyncio
async def test_create_rejects_backup_destination_inside_live_database_directory(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "data"
    operational = await _database(source_dir / "operational.sqlite3")
    audit = await _database(source_dir / "audit.sqlite3")

    with pytest.raises(BackupError) as error:
        await BackupService().create(
            sources=(
                BackupSource("operational", operational),
                BackupSource("audit", audit),
            ),
            output_dir=source_dir / "backups",
            deployment_id="home-lab",
        )

    assert error.value.code == "backup_destination_invalid"
    await operational.close()
    await audit.close()


@pytest.mark.asyncio
async def test_create_surfaces_directory_fsync_failure(tmp_path: Path) -> None:
    source_dir = tmp_path / "data"
    operational = await _database(source_dir / "operational.sqlite3")
    audit = await _database(source_dir / "audit.sqlite3")

    def fail_open(*_: object) -> int:
        raise OSError("directory open failed")

    monkeypatch = pytest.MonkeyPatch()
    open_file = backup_module.os.open

    def fail_directory_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if "dir_fd" in kwargs:
            return open_file(path, flags, *args, **kwargs)
        return fail_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(backup_module, "_fsync_file", lambda _: None)
    monkeypatch.setattr(backup_module.os, "open", fail_directory_open)
    try:
        with pytest.raises(BackupError) as error:
            await BackupService().create(
                sources=(
                    BackupSource("operational", operational),
                    BackupSource("audit", audit),
                ),
                output_dir=tmp_path / "backups",
                deployment_id="home-lab",
            )
    finally:
        monkeypatch.undo()

    assert error.value.code == "backup_destination_invalid"
    await operational.close()
    await audit.close()
