import json
from pathlib import Path

import pytest

from domoai.config.settings import Settings
from domoai.mcp.stdio import build_configured_server


@pytest.mark.asyncio
async def test_configured_server_reports_real_loaded_policies(tmp_path: Path) -> None:
    policy_path = tmp_path / "policies.toml"
    policy_path.write_text(
        "[[policies]]\n"
        'id = "deny-vacation-mode"\n'
        'action = "deny"\n'
        "priority = 100\n"
        "[policies.target]\n"
        'device_id = "cover.garage_main"\n',
        encoding="utf-8",
    )
    runtime, server = await build_configured_server(
        Settings(database_path=tmp_path / "stdio-policies.sqlite3", policy_config_path=policy_path)
    )
    try:
        contents = list(await server.read_resource("domotics://policies"))
        assert len(contents) == 1
        payload = json.loads(contents[0].content)
        assert [policy["id"] for policy in payload["policies"]] == ["deny-vacation-mode"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_configured_server_reports_empty_policies_when_unconfigured(tmp_path: Path) -> None:
    runtime, server = await build_configured_server(
        Settings(database_path=tmp_path / "stdio-no-policies.sqlite3")
    )
    try:
        contents = list(await server.read_resource("domotics://policies"))
        assert len(contents) == 1
        payload = json.loads(contents[0].content)
        assert payload["policies"] == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_configured_server_reports_a_well_formed_metrics_snapshot(tmp_path: Path) -> None:
    runtime, server = await build_configured_server(
        Settings(database_path=tmp_path / "stdio-metrics.sqlite3")
    )
    try:
        contents = list(await server.read_resource("domotics://metrics"))
        assert len(contents) == 1
        payload = json.loads(contents[0].content)
        assert payload["available"] is True
        assert payload["schema_version"] == "v1"
        assert "adapter_health" in payload
        assert payload["event_queue_depth"] == {"bulk": 0, "priority": 0}
        assert payload["dropped_events_by_adapter"] == {}
        assert payload["dropped_events_by_kind"] == {}
        assert payload["coalesced_events_total"] == 0
        assert payload["event_consumer_alive"] is False
        assert payload["scheduler_alive"] is False
        assert payload["plans_by_status"] == {"pending": 0, "executing": 0, "unknown": 0}
    finally:
        await runtime.close()
