"""Read-only surface audit; only in-memory doubles, no real DB/network/hardware."""

import asyncio
import json

import httpx
from mcp.server.auth.provider import AccessToken

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.application.authority import AuthorityPolicy
from domoai.domain.models import AdapterSnapshot, AuthorityContext, PrincipalRole
from domoai.management.api import create_management_app
from domoai.mcp.resources import coverage_snapshot, inventory_snapshot
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry


class Manager:
    def __init__(self):
        self.audit = AuditLog()
        self.calls = []

    async def disable(self, connection_id):
        self.calls.append(str(connection_id))
        return {"disabled": str(connection_id)}


class HAClient:
    async def fetch_states(self):
        return [
            {"entity_id": "switch.good", "state": "on", "attributes": {}},
            {
                "entity_id": "light.off",
                "state": "off",
                "attributes": {"brightness": None, "supported_color_modes": ["brightness"]},
            },
        ]


async def main():
    authority = AuthorityContext(
        principal_id="restricted-agent",
        roles=[PrincipalRole.OPERATOR],
        device_ids=["light.allowed"],
        area_ids=["allowed"],
    )

    async def verify(_):
        return AccessToken(
            token="test",
            client_id="restricted-agent",
            scopes=["mutate"],
            claims={"authority": authority.model_dump(mode="json")},
        )

    manager = Manager()
    app = create_management_app(manager, auth_verifier=verify, authority_policy=AuthorityPolicy())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/v1/connections/11111111-1111-1111-1111-111111111111/disable",
            headers={"Authorization": "Bearer test"},
        )
        assert response.status_code == 200 and len(manager.calls) == 1
        print("MANAGEMENT_RESTRICTED_TOKEN:", response.status_code, response.text, manager.calls)
    registry = DeviceRegistry()
    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "light.one",
                    "device_id": "one",
                    "name": "Lamp",
                    "domain": "light",
                    "semantic_type": "light",
                    "area_id": "allowed",
                    "available": False,
                    "capabilities": [
                        {
                            "name": "power",
                            "kind": "boolean",
                            "readable": True,
                            "writable": True,
                            "commands": ["turn_on", "turn_off"],
                        }
                    ],
                }
            ],
            source_states=[],
            areas=[{"id": "allowed", "name": "Allowed"}, {"id": "private", "name": "Private room"}],
        ),
        "mqtt",
    )
    snapshot = coverage_snapshot(registry, runtime_revision="1", active_provider_ids=("mqtt",))
    route = registry.routes_for(registry.devices[0].id, "power")[0]
    assert route.available is False and snapshot["routes"][0]["status"] == "active"
    print("COVERAGE_UNAVAILABLE_ROUTE:", route.available, json.dumps(snapshot))
    inventory = inventory_snapshot(registry, runtime_revision="1", refreshed_at=None, devices=[])
    assert inventory["devices"] == [] and any(a["id"] == "private" for a in inventory["areas"])
    print("INVENTORY_EMPTY_VISIBLE:", json.dumps(inventory))
    try:
        await HomeAssistantProvider(HAClient())._load_snapshot()
    except TypeError as error:
        assert "NoneType" in str(error)
        print("HA_NULL_BRIGHTNESS_ABORTS_SNAPSHOT:", type(error).__name__, str(error))
    else:
        raise AssertionError("Expected current HA normalization failure")


asyncio.run(main())
