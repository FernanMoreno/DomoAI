import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.domain.coordination import FencingToken, LeaseScope


def _token(epoch: int = 3) -> FencingToken:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    return FencingToken(
        scope=LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        owner_id="host-a",
        epoch=epoch,
        lease_id="lease-a",
        issued_at=now,
        expires_at=now + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_jsonl_bridge_round_trips_a_gateway_probe(tmp_path: Path) -> None:
    from domoai.hil.multihost import JsonlFencingGatewayBridge

    bridge_script = tmp_path / "gateway_bridge.py"
    bridge_script.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'schema_version': 'v1', 'probe_id': request['probe_id'], "
        "'accepted': True, 'observed_epoch': request['fencing_epoch']}))\n",
        encoding="utf-8",
    )
    bridge = JsonlFencingGatewayBridge((sys.executable, str(bridge_script)))

    result = await bridge.probe(_token(), safe_command="safe-noop")

    assert result.accepted is True
    assert result.observed_epoch == 3


@pytest.mark.asyncio
async def test_jsonl_bridge_rejects_malformed_or_mismatched_response(tmp_path: Path) -> None:
    from domoai.hil.multihost import (
        GatewayFencingProbeError,
        JsonlFencingGatewayBridge,
    )

    bridge_script = tmp_path / "bad_gateway_bridge.py"
    bridge_script.write_text("print('not-json')\n", encoding="utf-8")
    bridge = JsonlFencingGatewayBridge((sys.executable, str(bridge_script)))

    with pytest.raises(GatewayFencingProbeError, match="invalid JSON"):
        await bridge.probe(_token(), safe_command="safe-noop")
