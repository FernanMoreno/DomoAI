from __future__ import annotations

from pathlib import Path

from domoai.lab.bridge_supervisor import BridgeState, BridgeStatus
from domoai.lab.runner import SERVICE_NAMES, SERVICE_PROFILES

ROOT = Path(__file__).parents[2]


def test_knx_bridge_is_a_local_service_and_not_a_second_compose_gateway() -> None:
    compose = (ROOT / "dev/lab/compose.yaml").read_text(encoding="utf-8")

    assert "knx-bridge" in SERVICE_NAMES
    assert "knx-bridge" not in SERVICE_PROFILES
    assert compose.count("  knx-gateway:") == 1


def test_bridge_status_contract_has_non_ready_failure_states() -> None:
    assert {item.value for item in BridgeState} >= {
        "starting",
        "ready",
        "degraded",
        "failed",
        "stopped",
    }
    payload = BridgeStatus(state=BridgeState.DEGRADED).to_dict()
    assert payload["schema_version"] == "v1"
    assert payload["state"] == "degraded"
    assert payload["pid"] is None


def test_ready_status_contract_carries_independent_readback_evidence() -> None:
    payload = BridgeStatus(
        state=BridgeState.READY,
        knx_readback_at="2026-08-30T10:00:02Z",
        knx_readback_ok=True,
    ).to_dict()

    assert payload["knx_readback_ok"] is True
    assert payload["knx_readback_at"] == "2026-08-30T10:00:02Z"


def test_bridge_entrypoint_exposes_supervised_status_argument() -> None:
    source = (ROOT / "dev/lab/battery/knx_bridge.py").read_text(encoding="utf-8")
    assert "--status-file" in source
    assert "BridgeStatusStore" in source
