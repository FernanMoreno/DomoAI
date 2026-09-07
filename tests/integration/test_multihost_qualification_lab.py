"""Docker-backed qualification of the disposable multi-host lab."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from domoai.domain.multihost_qualification import MultiHostQualificationEvidence

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_multihost_qualification_lab.sh"
RUNNER_DOCKERFILE = ROOT / "deploy" / "multihost" / "qualification" / "runner" / "Dockerfile"


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def test_lab_runner_uses_a_local_ephemeral_image_without_a_venv_mount() -> None:
    shell = RUNNER.read_text(encoding="utf-8")

    assert RUNNER_DOCKERFILE.is_file()
    assert 'docker build --tag "$runner_image" --file "$runner_dockerfile" "$root_dir"' in shell
    assert 'docker run --rm --network "$network_name"' in shell
    assert '--user "$(id -u):$(id -g)"' in shell
    assert "failover_diagnostics" in shell
    assert "--tail 60 postgres-1 postgres-2 postgres-3 postgres-writer" in shell
    assert ".venv" not in shell
    assert "wait_for_replica_members" in shell
    assert 'patroni_primary_status "$service" replica' in shell
    assert 'patroni_primary_status "$service" sync' in shell
    assert "sync_ready" in shell


@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")
def test_lab_runner_fails_over_to_a_new_primary_and_requalifies() -> None:
    result = subprocess.run(
        [str(RUNNER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=420,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(records) == 2
    failover = records[0]
    assert re.fullmatch(r"postgres-[123]", failover["initial_primary"])
    assert re.fullmatch(r"postgres-[123]", failover["post_failover_primary"])
    assert failover["initial_primary"] != failover["post_failover_primary"]

    evidence = MultiHostQualificationEvidence.model_validate(records[1])
    assert evidence.qualification_environment == "lab"
    assert evidence.status == "passed"
    assert {check.check_id: check.status for check in evidence.checks} == {
        "etcd_quorum": "passed",
        "etcd_lease_renewal": "passed",
        "etcd_takeover": "passed",
        "postgres_primary": "passed",
        "postgres_sync_replica": "passed",
        "gateway_current_epoch": "passed",
        "gateway_stale_epoch": "passed",
        "gateway_replay_epoch": "passed",
    }
