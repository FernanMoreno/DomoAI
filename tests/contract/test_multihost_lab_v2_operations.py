from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_multihost_lab_v2.sh"
BACKUP = ROOT / "deploy" / "multihost" / "qualification" / "backup_restore.sh"
LOAD = ROOT / "deploy" / "multihost" / "qualification" / "load_generator.py"


def test_backup_drill_is_project_scoped_and_checks_all_sentinels() -> None:
    script = BACKUP.read_text(encoding="utf-8")

    assert "pg_dump" in script
    assert "psql" in script
    assert "physical_intents" in script
    assert "audit_outbox" in script
    assert "operational_metric_history" in script
    assert "com.domoai.lab=true" in script
    assert "postgresql://" not in script


def test_bounded_load_and_runner_emit_secret_safe_lab_records() -> None:
    load = LOAD.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")

    assert "HouseholdWorkQueues" in load
    assert "max_per_household" in load
    assert "max_total" in load
    assert "metric_history_bounded" in runner
    assert '"qualification_environment": "lab"' in runner
    assert "password" not in load.lower()
    assert "lease_id" not in runner


def test_v2_runner_starts_hosts_only_after_database_writer_is_ready() -> None:
    runner = RUNNER.read_text(encoding="utf-8")

    host_services = "compose up --detach host-a host-b"
    base_start = runner.index("compose up --detach --build --quiet-pull")
    base_end = runner.index(">/dev/null", base_start)
    base_command = runner[base_start:base_end]

    assert "etcd-1 etcd-2 etcd-3" in base_command
    assert "postgres-1 postgres-2 postgres-3 postgres-writer" in base_command
    assert host_services in runner
    writer_wait = runner.index("wait_for_database_writer\n", base_start)
    assert base_start < writer_wait
    assert writer_wait < runner.index(host_services)
