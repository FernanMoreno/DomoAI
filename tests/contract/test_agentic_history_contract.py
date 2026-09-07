from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.domain.models import SourceRef, StateSnapshot, StateStatus
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.persistence.repositories import StateHistoryRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def _structured(result: object) -> dict[str, object]:
    protocol_content = getattr(result, "structuredContent", None)
    if isinstance(protocol_content, dict):
        return protocol_content

    if isinstance(result, tuple) and len(result) > 1:
        assert isinstance(result[1], dict)
        return result[1]
    assert isinstance(result, dict)
    return result


@pytest.mark.asyncio
async def test_get_history_is_read_only_and_returns_versioned_samples(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "history-mcp.sqlite3")
    await database.initialize()
    try:
        now = datetime(2026, 9, 5, 12, tzinfo=UTC)
        history = StateHistoryRepository(database, household_id="default")
        await history.append(
            [
                StateSnapshot(
                    device_id="light.kitchen",
                    capability="brightness",
                    value=42,
                    observed_at=now,
                    received_at=now,
                    status=StateStatus.CURRENT,
                    source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
                )
            ]
        )
        adapter = SimulatedHomeAdapter()
        registry = DeviceRegistry()
        state_store = StateStore()
        audit = AuditLog()
        discovery = DiscoveryService(adapter, registry, state_store, audit)
        plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
        context = DomoticsMcpContext(
            discovery=discovery,
            state_service=StateService(state_store),
            facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
            registry=registry,
            policies=[],
            state_history_repository=history,
        )
        server = create_domotics_server(context)

        result = _structured(
            await server.call_tool(
                "get_history",
                {
                    "devices": ["light.kitchen"],
                    "start": "2026-09-05T11:00:00+00:00",
                    "end": "2026-09-05T13:00:00+00:00",
                },
            )
        )

        assert result["schema_version"] == "v1"
        assert result["household_id"] == "default"
        assert result["count"] == 1
        assert result["samples"][0]["snapshot"]["value"] == 42
        assert {tool.name for tool in await server.list_tools()}.__contains__("get_history")

        context.state_history_repository = None
        unavailable = _structured(
            await server.call_tool("get_history", {"devices": ["light.kitchen"]})
        )
        assert unavailable["error"]["message"] == "Request could not be processed"
        assert adapter.calls == []
    finally:
        await database.close()
