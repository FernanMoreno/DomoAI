import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from domoai.admin.cli import main
from domoai.persistence.backup import BackupService, BackupSource
from domoai.persistence.sqlite import SQLiteDatabase


async def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_admin_cli_emits_safe_json_for_create_and_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_dir = tmp_path / "source"
    operational = await _database(source_dir / "domoai.sqlite3")
    audit = await _database(source_dir / "domoai-audit.sqlite3")
    await operational.close()
    await audit.close()

    exit_code = await asyncio.to_thread(
        main,
        [
            "backup",
            "create",
            "--database",
            str(source_dir / "domoai.sqlite3"),
            "--audit-database",
            str(source_dir / "domoai-audit.sqlite3"),
            "--output-dir",
            str(tmp_path / "backups"),
            "--deployment-id",
            "home-lab",
        ],
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "created"
    assert output["members"] == ["audit", "operational"]
    assert str(tmp_path) not in json.dumps(output)

    verify_code = await asyncio.to_thread(
        main,
        [
            "backup",
            "verify",
            "--backup-dir",
            str(tmp_path / "backups" / output["backup_id"]),
        ],
    )
    verify_output = json.loads(capsys.readouterr().out)
    assert verify_code == 0
    assert verify_output["status"] == "verified"
    assert verify_output["backup_id"] == output["backup_id"]


def test_admin_cli_returns_stable_redacted_error_for_invalid_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["backup", "verify", "--backup-dir", str(tmp_path / "does-not-exist")])

    output = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert output == {"error": {"code": "backup_manifest_invalid"}}
    assert str(tmp_path) not in json.dumps(output)


def test_admin_cli_rejects_missing_source_without_creating_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "missing.sqlite3"
    audit_database = tmp_path / "missing-audit.sqlite3"
    exit_code = main(
        [
            "backup",
            "create",
            "--database",
            str(database),
            "--audit-database",
            str(audit_database),
            "--output-dir",
            str(tmp_path / "backups"),
            "--deployment-id",
            "home-lab",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert output == {"error": {"code": "backup_source_unavailable"}}
    assert not database.exists()
    assert not audit_database.exists()


def test_admin_cli_rejects_existing_uninitialized_database_without_mutating_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_dir = tmp_path / "source"
    database = source_dir / "empty.sqlite3"
    audit_database = source_dir / "empty-audit.sqlite3"
    source_dir.mkdir()
    database.touch()
    audit_database.touch()

    exit_code = main(
        [
            "backup",
            "create",
            "--database",
            str(database),
            "--audit-database",
            str(audit_database),
            "--output-dir",
            str(tmp_path / "backups"),
            "--deployment-id",
            "home-lab",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert output == {"error": {"code": "backup_source_unavailable"}}
    connection = sqlite3.connect(database)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_manifest_does_not_contain_database_contents_or_credentials(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    operational = await _database(source_dir / "domoai.sqlite3")
    audit = await _database(source_dir / "domoai-audit.sqlite3")
    connection = operational.connection
    connection.execute("CREATE TABLE secret_probe (value TEXT NOT NULL)")
    connection.execute("INSERT INTO secret_probe VALUES (?)", ("super-secret-token",))
    connection.commit()
    manifest = await BackupService().create(
        sources=(BackupSource("operational", operational), BackupSource("audit", audit)),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )

    manifest_text = (tmp_path / "backups" / manifest.backup_id / "manifest.json").read_text(
        encoding="utf-8"
    )
    assert "super-secret-token" not in manifest_text
    assert "secret_probe" not in manifest_text
    await operational.close()
    await audit.close()
