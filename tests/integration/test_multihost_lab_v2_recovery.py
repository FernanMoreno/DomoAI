"""Focused Docker recovery assertions for the v2 lab output."""

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


@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")
def test_v2_recovery_report_proves_idempotency_outbox_restore_and_bounds() -> None:
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
    by_id = {record["scenario_id"]: record for record in records if "scenario_id" in record}

    assert by_id["crash-replay"]["observations"]["duplicate_accepted_commands"] == 0
    assert by_id["crash-replay"]["observations"]["outbox_delivery_count"] == 1
    assert by_id["backup-restore"]["observations"]["sentinels_preserved"] is True
    assert by_id["bounded-load"]["observations"]["max_queue_depth"] <= 8
    encoded = result.stdout.lower()
    assert "postgresql://" not in encoded
    assert "password" not in encoded
