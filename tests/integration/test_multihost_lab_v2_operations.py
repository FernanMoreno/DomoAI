from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOAD = ROOT / "deploy" / "multihost" / "qualification" / "load_generator.py"


def test_bounded_load_generator_enforces_household_and_global_limits() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(LOAD),
            "--max-per-household",
            "8",
            "--max-total",
            "8",
            "--requests",
            "24",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed"
    assert report["max_queue_depth"] <= 8
    assert report["rejected_requests"] == 16
    assert report["metric_history_bounded"] is True
