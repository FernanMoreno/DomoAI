from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import AdapterSnapshot
from tests.fixtures.multi_adapter import RecordingAdapter


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_composition_reports_observed_battery_without_dispatch_authority(
    tmp_path,
) -> None:
    now = datetime.now(UTC)
    snapshot = AdapterSnapshot(
        source_entities=[
            {
                "entity_id": "sensor.battery_soc",
                "source_device_id": "battery-1",
                "canonical_id": "lab.battery",
                "identity_keys": ["fixture:battery-1"],
                "connections": ["fixture:battery-1"],
                "name": "Lab Battery SOC",
                "domain": "sensor",
                "semantic_type": "energy",
                "capabilities": [
                    {
                        "name": "battery.soc",
                        "kind": "number",
                        "unit": "%",
                        "readable": True,
                        "writable": False,
                    }
                ],
                "available": True,
            }
        ],
        source_states=[
            {
                "entity_id": "sensor.battery_soc",
                "capability": "battery.soc",
                "value": 50.0,
                "unit": "%",
                "available": True,
                "observed_at": now,
            }
        ],
    )
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "runtime.sqlite3"),
        adapter=RecordingAdapter("fixture", snapshot),
    )

    try:
        assert runtime.battery_qualification == "unsupported"
        assert runtime.battery_operational_status == "observed-only"
        assert runtime.dispatchable_battery_binding is None
    finally:
        await runtime.close()
