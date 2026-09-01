"""Opt-in composition test for the real Windows/WSL KNX battery path."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from domoai.lab.bridge_supervisor import BridgeStatusStore, BridgeSupervisorConfig
from domoai.lab.runner import LabConfig, LabRunner, parse_env_file

ROOT = Path(__file__).parents[2]


def test_live_knx_lab_bridge_composition() -> None:
    if os.getenv("DOMOAI_LIVE_KNX_LAB_COMPOSITION_ENABLE") != "1":
        pytest.skip("set DOMOAI_LIVE_KNX_LAB_COMPOSITION_ENABLE=1 for the real KNX lab")

    environment = dict(os.environ)
    environment.update(parse_env_file(ROOT / "dev" / "lab" / ".env"))
    if not environment.get("DOMOAI_KNX_GATEWAY_HOST"):
        pytest.skip("DOMOAI_KNX_GATEWAY_HOST is required for the real KNX lab")

    config = BridgeSupervisorConfig.from_environment(
        ROOT, environment, python_executable=sys.executable
    )
    if not config.resolved_mapping_path.is_file():
        pytest.skip("the configured KNX battery mapping is not available")

    runner = LabRunner(LabConfig(repo_root=ROOT))
    try:
        result = runner.up(("mqtt", "battery", "knx-bridge"))
        status = BridgeStatusStore(config.status_path).read()
        assert result == 0, status
        assert status is not None
        assert status.state.value == "ready"
        assert status.knx_readback_ok is True
    finally:
        runner.down()
