from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import AdapterSnapshot
from domoai.runtime.clock import FixedClock
from tests.fixtures.multi_adapter import RecordingAdapter


@pytest.mark.asyncio
async def test_runtime_factory_persists_the_shared_commissioning_report(
    tmp_path: Path,
) -> None:
    adapter = RecordingAdapter(
        "fixture",
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "fixture.battery",
                    "source_device_id": "battery-1",
                    "canonical_id": "garage.battery",
                    "identity_keys": ["fixture:battery-1"],
                    "connections": ["fixture:bus:1"],
                    "name": "Battery",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.soc",
                            "kind": "number",
                            "unit": "kWh",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery.power",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery.capacity",
                            "kind": "number",
                            "unit": "kWh",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery_control",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": True,
                            "commands": ["charge", "discharge", "stop"],
                        },
                    ],
                    "available": True,
                }
            ],
            source_states=[],
        ),
    )
    report_path = tmp_path / "commissioning.json"
    settings = Settings(
        database_path=tmp_path / "runtime.sqlite3",
        commissioning_manifest_path=report_path,
    )

    runtime = await build_runtime(
        settings,
        adapter=adapter,
        clock=FixedClock(datetime(2026, 8, 31, tzinfo=UTC)),
    )
    try:
        assert runtime.commissioning_service is not None
        assert runtime.commissioning_report is not None
        assert runtime.commissioning_report.report_digest
        assert report_path.is_file()
        assert (
            runtime.commissioning_report.candidates[0].canonical_device_id
            == "garage.battery"
        )
    finally:
        await runtime.close()
