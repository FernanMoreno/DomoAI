"""Docker-backed exercises for the second-generation multi-host lab."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_multihost_lab_v2.sh"


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


def test_v2_runner_declares_all_bounded_scenarios() -> None:
    shell = RUNNER.read_text(encoding="utf-8")

    for scenario in (
        "ownership-race",
        "partition-takeover",
        "crash-replay",
        "control-plane-loss",
        "database-primary-failover",
        "secure-rotation",
        "backup-restore",
        "bounded-load",
    ):
        assert scenario in shell
    assert "com.domoai.lab" in shell
    assert "--profile v2" in shell
    assert "qualification_environment" in shell
    assert "down --volumes --remove-orphans" in shell


@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")
def test_v2_runner_completes_race_partition_and_recovery_matrix() -> None:
    result = subprocess.run(
        [str(RUNNER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert records
    by_id = {
        record["scenario_id"]: record
        for record in records
        if "scenario_id" in record and record["scenario_id"] != "summary"
    }
    assert set(by_id) == {
        "ownership-race",
        "partition-takeover",
        "crash-replay",
        "control-plane-loss",
        "database-primary-failover",
        "secure-rotation",
        "backup-restore",
        "bounded-load",
    }
    assert all(record["status"] == "passed" for record in by_id.values())
    assert all(record["qualification_environment"] == "lab" for record in by_id.values())
    assert by_id["ownership-race"]["observations"]["unauthorized_writes"] == 0
    assert by_id["partition-takeover"]["observations"]["stale_epoch_rejected"] is True

    summary = records[-1]
    assert summary["qualification_environment"] == "lab"
    assert summary["cleanup"]["containers"] == "removed"
