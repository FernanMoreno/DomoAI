import json

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.privacy import PrivacyService
from domoai.application.state_service import StateService
from domoai.domain.models import AuthorityContext
from domoai.domain.privacy import HouseholdDataPolicy, PrivacyCategory
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.persistence.privacy import SQLitePrivacyStore
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
async def test_mcp_privacy_tools_are_scoped_redacted_and_audited(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "privacy-mcp.sqlite3")
    await database.initialize()
    try:
        authority = AuthorityContext()
        database.connection.execute(
            "INSERT INTO plans (id, payload, updated_at) VALUES (?, ?, ?)",
            (
                "privacy-plan",
                json.dumps(
                    {
                        "authority": authority.model_dump(mode="json"),
                        "id": "privacy-plan",
                        "api_token": "must-not-leak",
                    }
                ),
                "2026-09-04T00:00:00+00:00",
            ),
        )
        database.connection.commit()

        adapter = SimulatedHomeAdapter()
        registry = DeviceRegistry()
        state_store = StateStore()
        audit = AuditLog()
        discovery = DiscoveryService(adapter, registry, state_store, audit)
        await discovery.refresh()
        plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
        policy = HouseholdDataPolicy(
            authority=authority,
            exportable_categories=[PrivacyCategory.PLANS],
            deletable_categories=[PrivacyCategory.PLANS],
            immutable_categories=[PrivacyCategory.AUDIT],
            retention_days=90,
        )
        context = DomoticsMcpContext(
            discovery=discovery,
            state_service=StateService(state_store),
            facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
            registry=registry,
            policies=[],
            privacy_service=PrivacyService(
                SQLitePrivacyStore(database), audit=audit.append
            ),
            privacy_policy=policy,
        )
        server = create_domotics_server(context)

        tools = {tool.name for tool in await server.list_tools()}
        exported = _structured(
            await server.call_tool("export_household_data", {"categories": ["plans"]})
        )
        deleted = _structured(
            await server.call_tool(
                "delete_household_data",
                {"categories": ["plans"], "request_id": "privacy-mcp-delete"},
            )
        )

        assert {"export_household_data", "delete_household_data"} <= tools
        assert exported["record_count"] == 1
        assert "api_token" not in json.dumps(exported)
        assert deleted["deleted_counts"] == {"plans": 1}
        assert audit.events[-1].event_type == "privacy_data_deleted"
        assert audit.events[-1].authority.household_id == "default"
    finally:
        await database.close()
