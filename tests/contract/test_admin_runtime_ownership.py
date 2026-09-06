from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from domoai.admin.cli import main
from domoai.persistence.repositories import RuntimeOwnershipRepository
from domoai.persistence.sqlite import SQLiteDatabase


async def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_admin_releases_exact_stale_owner_and_allows_next_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "gateway.sqlite3"
    database = await _database(database_path)
    await RuntimeOwnershipRepository(database).acquire(
        deployment_id="home-lab",
        owner_id="stale-owner",
        config_digest="sha256:config",
    )
    await database.close()

    exit_code = await asyncio.to_thread(
        main,
        [
            "runtime",
            "release-stale-owner",
            "--database",
            str(database_path),
            "--deployment-id",
            "home-lab",
            "--owner-id",
            "stale-owner",
        ],
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {"deployment_id": "home-lab", "status": "released"}

    reopened = await _database(database_path)
    try:
        await RuntimeOwnershipRepository(reopened).acquire(
            deployment_id="home-lab",
            owner_id="next-owner",
            config_digest="sha256:config",
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_admin_refuses_release_when_advisory_lock_is_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "gateway.sqlite3"
    database = await _database(database_path)
    await RuntimeOwnershipRepository(database).acquire(
        deployment_id="home-lab",
        owner_id="live-owner",
        config_digest="sha256:config",
    )
    live_lock = database.advisory_lock()
    live_lock.acquire()
    try:
        exit_code = await asyncio.to_thread(
            main,
            [
                "runtime",
                "release-stale-owner",
                "--database",
                str(database_path),
                "--deployment-id",
                "home-lab",
                "--owner-id",
                "live-owner",
            ],
        )
    finally:
        live_lock.release()
        await database.close()

    output = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert output == {"error": {"code": "runtime_owner_active"}}


@pytest.mark.asyncio
async def test_admin_refuses_owner_mismatch_without_mutating_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "gateway.sqlite3"
    database = await _database(database_path)
    await RuntimeOwnershipRepository(database).acquire(
        deployment_id="home-lab",
        owner_id="current-owner",
        config_digest="sha256:config",
    )
    await database.close()

    exit_code = await asyncio.to_thread(
        main,
        [
            "runtime",
            "release-stale-owner",
            "--database",
            str(database_path),
            "--deployment-id",
            "home-lab",
            "--owner-id",
            "old-owner",
        ],
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert output == {"error": {"code": "runtime_owner_mismatch"}}

    reopened = await _database(database_path)
    try:
        row = reopened.connection.execute(
            """SELECT owner_id, status, uncertain
               FROM runtime_ownership WHERE deployment_id = 'home-lab'"""
        ).fetchone()
        assert row == ("current-owner", "active", 0)
    finally:
        await reopened.close()


def test_admin_rejects_missing_database_without_creating_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "missing.sqlite3"
    exit_code = main(
        [
            "runtime",
            "release-stale-owner",
            "--database",
            str(database_path),
            "--deployment-id",
            "home-lab",
            "--owner-id",
            "owner",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert output == {"error": {"code": "runtime_database_unavailable"}}
    assert not database_path.exists()
