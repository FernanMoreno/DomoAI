"""Native archive recovery against real PostgreSQL, never a dump/restore fake."""

import io
import json
import subprocess
import sys
import tarfile
import traceback
from uuid import uuid4

import docker
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from testcontainers.community.postgres import PostgresContainer

import domoai.persistence.backup as backup_module
from domoai.domain.errors import DomainError
from domoai.domain.household_preferences import (
    PreferenceMutation,
    PreferenceRestore,
    PreferenceSlotMutation,
)
from domoai.persistence.postgres import PostgresDatabase
from tests.fixtures.household_preferences import NOW, mutation
from tests.integration.test_household_preference_persistence import repository, set_value


@pytest.fixture(scope="module")
def postgres():
    try:
        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker unavailable; native PostgreSQL restore is unproved")
    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        yield container


@pytest.fixture
def databases(postgres):
    names = []
    dsn = postgres.get_connection_url(driver=None)
    with psycopg.connect(dsn, autocommit=True) as admin:

        def create():
            name = "native_" + uuid4().hex
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            names.append(name)
            return make_conninfo(dsn, dbname=name)

        yield create
        for name in names:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def container_runner(postgres):
    """Adapt transport only; pg_dump/pg_restore run inside the real server."""
    container = postgres.get_wrapped_container()

    def run(command, dsn=None, data=None):
        command = list(command)
        if data is not None:
            member = tarfile.TarInfo("native-input-" + uuid4().hex)
            member.size = len(data)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as bundle:
                bundle.addfile(member, io.BytesIO(data))
            container.put_archive("/tmp", buffer.getvalue())
            command.append("/tmp/" + member.name)
        environment = (
            {}
            if dsn is None
            else {
                "PGDATABASE": conninfo_to_dict(dsn)["dbname"],
                "PGUSER": "domoai",
                "PGPASSWORD": "domoai-test",
                "PGHOST": "127.0.0.1",
                "PGPORT": "5432",
            }
        )
        result = container.exec_run(command, environment=environment, demux=True)
        if result.exit_code:
            raise RuntimeError("PostgreSQL native tool failed")
        return result.output[0] or b""

    return run


@pytest.mark.parametrize("default_runner", [False, True])
async def test_native_archive_rotates_every_head_and_preserves_source(
    tmp_path,
    postgres,
    databases,
    monkeypatch,
    default_runner,
):
    source = PostgresDatabase(databases())
    await source.initialize()
    try:
        repo = await repository(source)
        initial, _ = await repo.snapshot()
        confirmed = await set_value(repo, initial)
        other = await repository(source, "home-b")
        other_initial, _ = await other.snapshot()
        other_confirmed = await set_value(other, other_initial)
        identity = other_confirmed.preference.preference_id
        deleted = await other.mutate(
            operation="delete",
            principal_id="resident-1",
            now=NOW,
            request=PreferenceSlotMutation(
                expected_version=other_confirmed.version,
                preference_id=identity,
                idempotency_key="delete",
            ),
        )
        historical = await other.mutate(
            operation="restore",
            principal_id="resident-1",
            now=NOW,
            request=PreferenceRestore(
                expected_version=deleted.version,
                preference_id=identity,
                idempotency_key="restore",
                source_revision=1,
                valid_from="2026-09-12T06:00:00+00:00",
                valid_until=None,
                timezone="UTC",
                provenance={"source_kind": "manual"},
            ),
        )
        await other.mutate(
            operation="erase",
            principal_id="resident-1",
            now=NOW,
            request=PreferenceSlotMutation(
                expected_version=historical.version,
                preference_id=identity,
                idempotency_key="erase",
            ),
        )
        preserved = {
            table: source.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in (
                "household_preferences",
                "household_preference_history",
                "household_preference_requests",
            )
        }
        source.connection.commit()
        runner = container_runner(postgres)
        if default_runner:

            def transport(command, *, input, capture_output, env, check, timeout):
                # Exercise the production runner's libpq translation and error
                # boundary, adapting only subprocess transport to real tools.
                assert capture_output and check and timeout > 0
                assert "domoai-test" not in " ".join(command)
                if command[0] == "pg_dump" or "--list" not in command:
                    assert env["PGPASSWORD"] == "domoai-test"
                    assert env["PGUSER"] == "domoai"
                    dsn = make_conninfo(
                        source.dsn, dbname=env["PGDATABASE"], sslmode=env["PGSSLMODE"]
                    )
                else:
                    dsn = None
                return subprocess.CompletedProcess(command, 0, runner(command, dsn, input))

            monkeypatch.setattr(backup_module.subprocess, "run", transport)
            service = backup_module.PostgresBackupService()
        else:
            service = backup_module.PostgresBackupService(runner=runner)
        archive = tmp_path / "archive"
        service.create(source_dsn=make_conninfo(source.dsn, sslmode="disable"), output_dir=archive)
        target_dsn = databases()
        service.restore(backup_dir=archive, target_dsn=make_conninfo(target_dsn, sslmode="disable"))
        restored = PostgresDatabase(target_dsn)
        await restored.open_existing()
        try:
            target_repo = await repository(restored)
            version, slots = await target_repo.snapshot()
            assert version.generation != confirmed.version.generation
            assert version.revision == 1 and slots == [confirmed.preference]
            assert (
                await repository(restored, "home-b")
            )._head().generation != other_initial.generation
            for table, rows in preserved.items():
                assert (
                    restored.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
                    == rows
                )
            restored.connection.commit()
            for stale, key in [(initial, "request-1"), (confirmed.version, "old-cas")]:
                with pytest.raises(DomainError) as conflict:
                    await set_value(target_repo, stale, key=key)
                assert conflict.value.code == "preference_conflict"
            with pytest.raises(DomainError) as replay:
                await target_repo.replay(
                    request=PreferenceMutation.model_validate(mutation(initial.model_dump())),
                    principal_id="resident-1",
                    fingerprint="unreachable-for-stale-generation",
                )
            assert replay.value.code == "preference_conflict"
            fresh = await set_value(target_repo, version, key="fresh")
            assert fresh.version.revision == 2
            await restored.close()
            await restored.open_existing()
            assert (await repository(restored))._head() == fresh.version
        finally:
            await restored.close()
        assert (await repo.snapshot())[0] == confirmed.version
        assert len((await repo.history())[1]) == 1
    finally:
        await source.close()


@pytest.mark.parametrize(
    "failure",
    [
        "corruption",
        "format",
        "tool",
        "rotation",
        "floor",
        "content",
        "schema",
        "payload",
        "references",
    ],
)
async def test_failed_restore_never_opens_or_retries_target(
    tmp_path,
    postgres,
    databases,
    monkeypatch,
    failure,
):
    source = PostgresDatabase(databases())
    await source.initialize()
    try:
        repo = await repository(source)
        initial, _ = await repo.snapshot()
        await set_value(repo, initial)
        if failure == "floor":
            source.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=999")
            source.connection.commit()
        if failure == "schema":
            source.connection.execute("DELETE FROM schema_migrations")
            source.connection.commit()
        if failure == "payload":
            source.connection.execute("UPDATE household_preferences SET payload='{}'")
            source.connection.commit()
        if failure == "references":
            row = source.connection.execute(
                "SELECT payload FROM household_preference_history"
            ).fetchone()
            entry = json.loads(row[0])
            entry["version"]["revision"] = 999
            source.connection.execute(
                "UPDATE household_preference_history SET payload=?", (json.dumps(entry),)
            )
            source.connection.commit()
        runner = container_runner(postgres)
        service = backup_module.PostgresBackupService(runner=runner)
        archive = tmp_path / "archive"
        service.create(source_dsn=source.dsn, output_dir=archive)
        target_dsn = databases()
        if failure == "corruption":
            payload = archive / "database.dump"
            data = payload.read_bytes()
            payload.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        if failure == "format":
            manifest = archive / "postgres-manifest.json"
            metadata = json.loads(manifest.read_text())
            metadata["version"] = 999
            manifest.write_text(json.dumps(metadata))
        if failure in {"tool", "content"}:

            def fail(command, dsn=None, data=None):
                if command[0] == "pg_restore" and "--list" not in command:
                    if failure == "tool":
                        raise RuntimeError("secret password DSN payload")
                    output = runner(command, dsn, data)
                    with psycopg.connect(dsn) as connection:
                        connection.execute("DELETE FROM household_preference_requests")
                    return output
                return runner(command, dsn, data)

            service = backup_module.PostgresBackupService(runner=fail)
        if failure == "rotation":

            def fail_rotation(connection):
                connection.execute("UPDATE household_preference_heads SET generation='partial'")
                raise RuntimeError("secret password DSN payload")

            monkeypatch.setattr(
                backup_module, "rotate_restored_preference_generations", fail_rotation
            )
        with pytest.raises(backup_module.BackupError) as error:
            service.restore(backup_dir=archive, target_dsn=target_dsn)
        assert "secret" not in str(error.value)
        if failure in {"corruption", "format"}:
            assert error.value.code == "postgres_backup_invalid_archive"
            with psycopg.connect(target_dsn) as connection:
                assert connection.execute(
                    "SELECT to_regclass('public.schema_migrations')"
                ).fetchone() == (None,)
        target = PostgresDatabase(target_dsn)
        with pytest.raises(RuntimeError):
            await target.open_existing()
        if failure:
            with pytest.raises(RuntimeError):
                await target.initialize()
            with pytest.raises(backup_module.BackupError):
                service.restore(backup_dir=archive, target_dsn=target_dsn)
            with psycopg.connect(target_dsn) as connection:
                assert connection.execute(
                    "SELECT state FROM domoai_restore_guard.status"
                ).fetchall() == [("restoring",)]
                if failure == "rotation":
                    assert connection.execute(
                        "SELECT generation FROM household_preference_heads"
                    ).fetchone() == (str(initial.generation),)
        assert source.connection.execute(
            "SELECT generation FROM household_preference_heads"
        ).fetchone() == (str(initial.generation),)
    finally:
        await source.close()


def test_default_tool_failure_redacts_actual_subprocess_stderr():
    with pytest.raises(backup_module.BackupError) as failure:
        backup_module._run_postgres_tool(
            (
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('secret-native-tool-payload'); sys.exit(1)",
            ),
            "dbname=fixture password=secret-connection",
        )
    visible = "".join(traceback.format_exception_only(failure.value))
    assert "secret-native-tool-payload" not in visible
    assert "secret-connection" not in visible
    assert failure.value.code == "postgres_backup_tool_failed"
    assert failure.value.__suppress_context__


@pytest.mark.parametrize(
    "existing_object",
    [
        pytest.param("CREATE SCHEMA pgtenant", id="user-schema-pg-prefix"),
        pytest.param("CREATE TYPE existing_data AS ENUM ('preserved')", id="type"),
        pytest.param('CREATE COLLATION public.review_existing FROM pg_catalog."C"', id="collation"),
        pytest.param(
            "CREATE TEXT SEARCH CONFIGURATION public.review_existing (COPY = pg_catalog.english)",
            id="text-search",
        ),
        pytest.param(
            "CREATE OPERATOR public.## (FUNCTION = pg_catalog.int4pl, "
            "LEFTARG = integer, RIGHTARG = integer)",
            id="operator",
        ),
    ],
)
async def test_existing_object_is_not_an_empty_restore_target(
    tmp_path,
    postgres,
    databases,
    existing_object,
):
    source = PostgresDatabase(databases())
    await source.initialize()
    try:
        service = backup_module.PostgresBackupService(runner=container_runner(postgres))
        archive = tmp_path / "archive"
        service.create(source_dsn=source.dsn, output_dir=archive)
        target = databases()
        with psycopg.connect(target) as connection:
            connection.execute(existing_object)
        try:
            service.restore(backup_dir=archive, target_dsn=target)
        except backup_module.BackupError as failure:
            assert failure.code == "postgres_restore_target_not_empty"
        else:
            # Demonstrate the actual unsafe opening, not merely missing error
            # wording: the initial implementation imports and marks this ready.
            opened = PostgresDatabase(target)
            await opened.open_existing()
            try:
                assert opened.connection.execute(
                    "SELECT state FROM domoai_restore_guard.status"
                ).fetchall() == [("ready",)]
            finally:
                await opened.close()
            pytest.fail("Restore reached ready and open_existing succeeded on occupied target")
        with psycopg.connect(target) as connection:
            assert connection.execute(
                "SELECT to_regnamespace('domoai_restore_guard')"
            ).fetchone() == (None,)
    finally:
        await source.close()


async def test_native_archive_accounts_for_user_schema_with_pg_prefix(
    tmp_path,
    postgres,
    databases,
):
    source = PostgresDatabase(databases())
    await source.initialize()
    try:
        source.connection.execute(
            "CREATE SCHEMA pgtenant; "
            "CREATE TABLE pgtenant.review_marker (value TEXT NOT NULL); "
            "INSERT INTO pgtenant.review_marker VALUES ('preserved')"
        )
        source.connection.commit()
        service = backup_module.PostgresBackupService(runner=container_runner(postgres))
        archive = tmp_path / "archive"
        service.create(source_dsn=source.dsn, output_dir=archive)
        metadata = json.loads((archive / "postgres-manifest.json").read_text())
        assert "pgtenant.review_marker" in metadata["tables"], (
            "Native archive omitted a user-schema table from its content evidence"
        )
        assert metadata["tables"]["pgtenant.review_marker"]["rows"] == 1
        target = databases()
        service.restore(backup_dir=archive, target_dsn=target)
        with psycopg.connect(target) as connection:
            assert connection.execute("SELECT value FROM pgtenant.review_marker").fetchall() == [
                ("preserved",)
            ]
        assert source.connection.execute("SELECT value FROM pgtenant.review_marker").fetchall() == [
            ("preserved",)
        ]
    finally:
        await source.close()


@pytest.mark.composition
async def test_restored_postgres_bootstraps_and_restarts_without_physical_writes(
    tmp_path,
    postgres,
    databases,
):
    from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
    from domoai.application.coordination import DeterministicLeaseCoordinator
    from domoai.application.runtime_factory import build_runtime
    from domoai.config.settings import Settings

    class CountingAdapter(SimulatedHomeAdapter):
        supports_fencing = True
        writes = 0

        async def execute(self, *args, **kwargs):
            self.writes += 1
            raise AssertionError("restore must never issue physical writes")

        async def execute_source(self, *args, **kwargs):
            self.writes += 1
            raise AssertionError("restore must never issue physical writes")

    source = PostgresDatabase(databases())
    await source.initialize()
    try:
        repo = await repository(source)
        initial, _ = await repo.snapshot()
        confirmed = await set_value(repo, initial)
        service = backup_module.PostgresBackupService(runner=container_runner(postgres))
        archive = tmp_path / "archive"
        service.create(source_dsn=source.dsn, output_dir=archive)
        with pytest.raises(backup_module.BackupError) as refusal:
            service.restore(backup_dir=archive, target_dsn=source.dsn)
        assert refusal.value.code == "postgres_restore_target_not_empty"
        target_dsn = databases()
        service.restore(backup_dir=archive, target_dsn=target_dsn)
        settings = Settings(
            multi_host_enabled=True,
            postgres_dsn=target_dsn,
            instance_id="native-restore",
            mcp_tenant_id="tenant-a",
            mcp_household_id="home-a",
            commissioning_manifest_path=tmp_path / "commissioning.json",
        )
        generation = None
        for _ in range(2):
            adapter = CountingAdapter()
            runtime = await build_runtime(
                settings,
                adapter=adapter,
                lease_coordinator=DeterministicLeaseCoordinator(),
            )
            try:
                # Read through the same owned PostgreSQL connection consumed
                # by the runtime's serialized preference service.
                assert runtime.household_preferences is not None
                version, slots = await runtime.household_preferences.repository.snapshot()
                if generation is None:
                    generation = version.generation
                assert version.generation == generation != confirmed.version.generation
                assert slots == [confirmed.preference]
                assert adapter.writes == 0
            finally:
                await runtime.close()
            assert adapter.writes == 0
        assert (await repo.snapshot())[0] == confirmed.version
    finally:
        await source.close()
