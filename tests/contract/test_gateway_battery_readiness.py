from __future__ import annotations

import json

import pytest

from domoai.config.settings import Settings
from domoai.mcp.health import readyz
from tests.contract.test_gateway_health_contract import _Runtime


@pytest.mark.asyncio
async def test_readyz_distinguishes_observed_battery_from_dispatch_qualification() -> None:
    runtime = _Runtime(Settings(), knx_connected=True)
    runtime.battery_operational_status = "observed-only"

    response = await readyz(runtime, object())  # type: ignore[arg-type]

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["physical"] == {
        "status": "ready",
        "battery_qualification": "unsupported",
        "battery_operational_status": "observed-only",
    }
    assert "physical_actuator_not_qualified" not in payload["reason_codes"]
