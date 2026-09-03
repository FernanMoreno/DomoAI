import sqlite3
from pathlib import Path

import pytest

import domoai.persistence.backup as backup_module
from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import Command, Plan, PlanStatus
from domoai.persistence.backup import BackupError, BackupService, BackupSource
from domoai.persistence.repositories import RuntimeOwnershipRepository
from domoai.persistence.serialized import SerializedStorageExecutor
from domoai.persistence.sqlite import SQLiteDatabase


async def _database(path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(path)
    await database.initialize()
    return database


def _marker(database: SQLiteDatabase, table: str, value: str) -> None:
    database.connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (value TEXT NOT NULL)")
    database.connection.execute(f"INSERT INTO {table} (value) VALUES (?)", (value,))
    database.connection.commit()


def _read_marker(path: Path, table: str) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute(f"SELECT value FROM {table}").fetchone()[0])
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_online_backup_uses_both_serialized_storage_lanes(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    operational = await _database(source_dir / "domoai.sqlite3")
    audit = await _database(source_dir / "domoai-audit.sqlite3")
    operational_storage = SerializedStorageExecutor(operation_timeout_seconds=3)
    audit_storage = SerializedStorageExecutor(operation_timeout_seconds=3)
    _marker(operational, "plans_probe", "plan-1")
    _marker(audit, "audit_probe", "event-1")

    try:
        manifest = await BackupService().create(
            sources=(
                BackupSource("operational", operational, operational_storage),
                BackupSource("audit", audit, audit_storage),
            ),
            output_dir=tmp_path / "backups",
            deployment_id="home-lab",
        )

        assert operational_storage.metrics.completed_count == 1
        assert audit_storage.metrics.completed_count == 1
        assert (
            _read_marker(
                tmp_path / "backups" / manifest.backup_id / "domoai.sqlite3", "plans_probe"
            )
            == "plan-1"
        )
        assert (
            _read_marker(
                tmp_path / "backups" / manifest.backup_id / "domoai-audit.sqlite3", "audit_probe"
            )
            == "event-1"
        )
    finally:
        await operational_storage.close()
        await audit_storage.close()
        await operational.close()
        await audit.close()


@pytest.mark.asyncio
async def test_runtime_composition_creates_backup_through_owned_lanes(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime-data"
    runtime = await build_runtime(
        Settings(database_path=data_dir / "domoai.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )

    try:
        manifest = await runtime.create_backup(tmp_path / "backups")
        assert manifest.deployment_id == "default"
        assert {member.name for member in manifest.members} == {"operational", "audit"}
        assert runtime.storage.metrics.completed_count > 0
        assert runtime.audit_storage.metrics.completed_count > 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_restore_stages_data_and_reopens_with_migrations(tmp_path: Path) -> None:
    source = await _database(tmp_path / "source" / "domoai.sqlite3")
    audit = await _database(tmp_path / "source" / "domoai-audit.sqlite3")
    _marker(source, "plans_probe", "plan-after-restore")
    _marker(audit, "audit_probe", "audit-after-restore")
    service = BackupService()
    manifest = await service.create(
        sources=(BackupSource("operational", source), BackupSource("audit", audit)),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    target = tmp_path / "restored-data"

    result = await service.restore(
        backup_dir=tmp_path / "backups" / manifest.backup_id,
        target_data_dir=target,
        deployment_id="home-lab",
    )

    assert result.backup_id == manifest.backup_id
    assert _read_marker(target / "domoai.sqlite3", "plans_probe") == "plan-after-restore"
    assert _read_marker(target / "domoai-audit.sqlite3", "audit_probe") == "audit-after-restore"
    reopened = await _database(target / "domoai.sqlite3")
    try:
        migrations = {
            row[0] for row in reopened.connection.execute("SELECT filename FROM schema_migrations")
        }
        assert "009_gateway_authority.sql" in migrations
    finally:
        await reopened.close()
        await source.close()
        await audit.close()


@pytest.mark.asyncio
async def test_restore_refuses_active_owner_and_leaves_target_unchanged(tmp_path: Path) -> None:
    source = await _database(tmp_path / "source" / "domoai.sqlite3")
    audit = await _database(tmp_path / "source" / "domoai-audit.sqlite3")
    _marker(source, "plans_probe", "source")
    service = BackupService()
    manifest = await service.create(
        sources=(BackupSource("operational", source), BackupSource("audit", audit)),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    target = tmp_path / "target"
    target_database = await _database(target / "domoai.sqlite3")
    _marker(target_database, "plans_probe", "must-survive")
    ownership = RuntimeOwnershipRepository(target_database)
    await ownership.acquire(
        deployment_id="home-lab",
        owner_id="live-runtime",
        config_digest="config-digest",
    )

    with pytest.raises(BackupError) as error:
        await service.restore(
            backup_dir=tmp_path / "backups" / manifest.backup_id,
            target_data_dir=target,
            deployment_id="home-lab",
        )

    assert error.value.code == "restore_ownership_active"
    assert _read_marker(target / "domoai.sqlite3", "plans_probe") == "must-survive"
    await target_database.close()
    await source.close()
    await audit.close()


@pytest.mark.asyncio
async def test_restore_refuses_active_owner_from_another_deployment(tmp_path: Path) -> None:
    source = await _database(tmp_path / "source" / "domoai.sqlite3")
    audit = await _database(tmp_path / "source" / "domoai-audit.sqlite3")
    service = BackupService()
    manifest = await service.create(
        sources=(BackupSource("operational", source), BackupSource("audit", audit)),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    target = tmp_path / "target"
    target_database = await _database(target / "domoai.sqlite3")
    _marker(target_database, "plans_probe", "must-survive")
    ownership = RuntimeOwnershipRepository(target_database)
    await ownership.acquire(
        deployment_id="another-deployment",
        owner_id="live-runtime",
        config_digest="config-digest",
    )

    with pytest.raises(BackupError) as error:
        await service.restore(
            backup_dir=tmp_path / "backups" / manifest.backup_id,
            target_data_dir=target,
            deployment_id="home-lab",
        )

    assert error.value.code == "restore_ownership_active"
    assert _read_marker(target / "domoai.sqlite3", "plans_probe") == "must-survive"
    await target_database.close()
    await source.close()
    await audit.close()


@pytest.mark.asyncio
async def test_restore_rejects_member_mutation_during_staging(tmp_path: Path) -> None:
    source = await _database(tmp_path / "source" / "domoai.sqlite3")
    audit = await _database(tmp_path / "source" / "domoai-audit.sqlite3")
    service = BackupService()
    manifest = await service.create(
        sources=(BackupSource("operational", source), BackupSource("audit", audit)),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    target = tmp_path / "target"
    target_database = await _database(target / "domoai.sqlite3")
    _marker(target_database, "plans_probe", "must-survive")
    await target_database.close()

    copy2 = backup_module.shutil.copy2

    def tamper_after_copy(source_path: Path, destination: Path) -> str:
        result = copy2(source_path, destination)
        if destination.parent.name.startswith(f".{target.name}.restore-"):
            with destination.open("ab") as handle:
                handle.write(b"staging-tamper")
        return result

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(backup_module.shutil, "copy2", tamper_after_copy)
    try:
        with pytest.raises(BackupError) as error:
            await service.restore(
                backup_dir=tmp_path / "backups" / manifest.backup_id,
                target_data_dir=target,
                deployment_id="home-lab",
            )
    finally:
        monkeypatch.undo()

    assert error.value.code == "restore_staging_failed"
    assert _read_marker(target / "domoai.sqlite3", "plans_probe") == "must-survive"
    await source.close()
    await audit.close()


@pytest.mark.asyncio
async def test_corrupt_restore_is_rejected_before_target_replacement(tmp_path: Path) -> None:
    source = await _database(tmp_path / "source" / "domoai.sqlite3")
    audit = await _database(tmp_path / "source" / "domoai-audit.sqlite3")
    service = BackupService()
    manifest = await service.create(
        sources=(BackupSource("operational", source), BackupSource("audit", audit)),
        output_dir=tmp_path / "backups",
        deployment_id="home-lab",
    )
    backup_dir = tmp_path / "backups" / manifest.backup_id
    with (backup_dir / "domoai.sqlite3").open("ab") as handle:
        handle.write(b"corrupted")
    target = tmp_path / "target"
    target_database = await _database(target / "domoai.sqlite3")
    _marker(target_database, "plans_probe", "keep-me")
    await target_database.close()

    with pytest.raises(BackupError) as error:
        await service.restore(
            backup_dir=backup_dir,
            target_data_dir=target,
            deployment_id="home-lab",
        )

    assert error.value.code == "backup_digest_mismatch"
    assert _read_marker(target / "domoai.sqlite3", "plans_probe") == "keep-me"
    await source.close()
    await audit.close()


@pytest.mark.asyncio
async def test_backup_while_runtime_is_running_restores_into_normal_bootstrap(
    tmp_path: Path,
) -> None:
    source_data = tmp_path / "source-runtime"
    running = await build_runtime(
        Settings(database_path=source_data / "domoai.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        manifest = await running.create_backup(tmp_path / "backups")
    finally:
        await running.close()

    target_data = tmp_path / "restored-runtime"
    await BackupService().restore(
        backup_dir=tmp_path / "backups" / manifest.backup_id,
        target_data_dir=target_data,
        deployment_id="default",
    )
    restarted = await build_runtime(
        Settings(database_path=target_data / "domoai.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        assert restarted.registry.devices
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_restored_executing_plan_is_recovered_without_adapter_replay(tmp_path: Path) -> None:
    source_data = tmp_path / "source-runtime"
    running = await build_runtime(
        Settings(database_path=source_data / "domoai.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )
    device_id = next(
        device.id for device in running.registry.devices if device.type.value == "light"
    )
    plan = Plan(
        id="restored-executing-plan",
        commands=[
            Command(
                id="restored-executing-command",
                device_id=device_id,
                command="set_brightness",
                value=50,
                unit="%",
                idempotency_key="restored-executing-idempotency",
            )
        ],
    )
    validated = running.plan_service.validate(plan)
    await running.plan_repository.save(
        validated.model_copy(update={"status": PlanStatus.EXECUTING})
    )
    try:
        manifest = await running.create_backup(tmp_path / "backups")
    finally:
        await running.close()

    target_data = tmp_path / "restored-runtime"
    await BackupService().restore(
        backup_dir=tmp_path / "backups" / manifest.backup_id,
        target_data_dir=target_data,
        deployment_id="default",
    )
    target_adapter = SimulatedHomeAdapter()
    restarted = await build_runtime(
        Settings(database_path=target_data / "domoai.sqlite3"),
        adapter=target_adapter,
    )
    try:
        recovered = await restarted.plan_repository.get(plan.id)
        assert recovered is not None
        assert recovered.status is PlanStatus.UNKNOWN
        assert target_adapter.calls == []
    finally:
        await restarted.close()
