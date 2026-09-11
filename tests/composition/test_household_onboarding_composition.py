"""P01 browser → independent SDK, and ACLs across actual loopback HTTP.

The browser case uses acquired HA HTTP fixture state. ACL cases deliberately seed
canonical synthetic state: they prove authorization, not physical acquisition.
Transfer/restart/concurrency regressions remain in test_household_onboarding.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

from domoai.application.state_service import StateService
from domoai.domain.coordination import LeaseScope
from domoai.domain.models import Device, SourceRef, StateSnapshot, StateStatus
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.runtime.identity import runtime_adapter_id
from tests.fixtures.household_onboarding_browser import browser_household
from tests.unit.persistence.test_connection_repository import _profile

pytestmark = [pytest.mark.composition, pytest.mark.asyncio]


@asynccontextmanager
async def _sdk(endpoint: str, token: str):
    # No ASGITransport: this SDK crosses the same listening socket as the browser.
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
        async with streamable_http_client(endpoint, http_client=client) as (read, write, _):
            try:
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.protocolVersion
                    yield session
                    await asyncio.sleep(0)
            finally:
                await read.aclose()
                await write.aclose()
    assert client.is_closed


async def _resource(session: ClientSession, name: str) -> dict:
    result = await session.read_resource(cast(AnyUrl, f"domotics://{name}"))
    text = getattr(result.contents[0], "text", None)
    assert isinstance(text, str)
    return json.loads(text)


async def _tool(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


async def test_browser_then_independent_sdk_reads_same_persisted_household(tmp_path, caplog):
    async with browser_household(tmp_path) as household:
        browser = await household.run_browser(tmp_path / "browser-evidence.json")
        assert browser["status"] == "PASS"
        evidence = browser["evidence"]
        runtime = household.gateway.runtime
        manager = runtime.connection_manager
        profile = await manager.repository.get(UUID(evidence["profile_id"]))
        assert profile is not None and profile.enabled
        assert [item.id for item in await manager.repository.list_all()] == [profile.id]
        assert household.read_token != household.management_token
        assert household.source.reads > 0 and household.source.commands == 0
        endpoint = household.base_url + runtime.settings.mcp_path
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {household.read_token}"}
        ) as client:
            status = await client.get(household.base_url + "/api/v1/onboarding/status")
        assert status.status_code == 200
        assert status.json()["client_connection"]["url"] == endpoint

        async with _sdk(endpoint, household.read_token) as session:
            tools = await session.list_tools()
            resources = await session.list_resources()
            assert {"get_state", "discover_devices", "execute_plan"} <= {
                tool.name for tool in tools.tools
            }
            assert "domotics://devices" in {str(item.uri) for item in resources.resources}
            discovery = await _tool(session, "discover_devices", {"refresh": False})
            assert [device["id"] for device in discovery["devices"]] == [evidence["device_id"]]
            runtime_evidence = await _resource(session, "runtime")
            state_result = await _tool(
                session,
                "get_state",
                {
                    "devices": [evidence["device_id"]],
                    "capabilities": [evidence["capability"]],
                    "allow_stale": False,
                },
            )
            assert state_result["runtime_revision"] == runtime_evidence["runtime_revision"]
            assert len(state_result["states"]) == 1
            state = state_result["states"][0]
            assert state["status"] == "current"
            for key in ("device_id", "capability", "value", "unit"):
                assert state[key] == evidence[key]
            assert (
                datetime.fromisoformat(state["observed_at"])
                == datetime.fromisoformat(evidence["timestamp"])
                == datetime.fromisoformat(household.source.states[0]["last_updated"])
            )
            assert state["source_ref"]["external_id"] == household.source.states[0]["entity_id"]
            assert (
                state["source_ref"]["adapter_id"]
                == evidence["source"]
                == runtime_adapter_id(profile.id)
            )
            denied = await _tool(
                session,
                "execute_plan",
                {
                    "plan_id": "read-client-must-not-resolve-plan",
                    "validation_digest": "read-client-must-not-execute",
                },
            )
            assert denied["error"]["code"] == "insufficient_scope"
            assert household.source.commands == 0

        session_manager = household.gateway.server.session_manager
        # SDK DELETE terminates transports; the SDK registry is cleared at app
        # lifespan exit, rather than when each client disconnects.
        assert all(
            transport._terminated and not transport._request_streams
            for transport in session_manager._server_instances.values()
        )
        report = {
            "browser": browser,
            "sdk_state": state,
            "runtime_revision": state_result["runtime_revision"],
            "execution_denied": denied["error"]["code"],
            "sdk_closed": True,
        }
        serialized = json.dumps(report) + caplog.text
        for secret in (household.management_token, household.read_token, household.source.token):
            assert secret not in serialized
    # Context exit has stopped both real HTTP servers and closed runtime storage/adapters.
    assert runtime.lifecycle.running_task_count == 0
    assert not runtime.adapter._connected
    assert runtime.database._connection is None
    assert not session_manager._server_instances
    assert session_manager._task_group is None
    assert household.source.commands == 0
    report["gateway_closed"] = True
    (tmp_path / "sdk-composition-evidence.json").write_text(json.dumps(report, indent=2))


async def _seed_acl_state(runtime):
    devices = [
        Device(
            id=identifier,
            name=identifier,
            type="sensor",
            protocol="fixture",
            area_id=area,
            capabilities=[
                dict(name=name, kind="number", readable=True, writable=False)
                for name in ("brightness", "temperature")
            ],
            source_refs=[dict(adapter_id="acl-fixture", external_id=identifier)],
        )
        for identifier, area in (
            ("fixture.allowed", "allowed"),
            ("fixture.foreign", "foreign"),
            ("fixture.unassigned", None),
        )
    ]
    runtime.registry.load_persisted(devices)
    now = datetime.now(UTC)
    for device in devices:
        for capability, value in (("brightness", 50), ("temperature", 21)):
            await runtime.state_store.save(
                StateSnapshot(
                    device_id=device.id,
                    capability=capability,
                    value=value,
                    observed_at=now,
                    received_at=now,
                    status=StateStatus.CURRENT,
                    source_ref=SourceRef(adapter_id="acl-fixture", external_id=device.id),
                )
            )
    return [device.model_dump(mode="json") for device in runtime.registry.devices]


def _add_reader(tmp_path: Path, name: str, **authority: Any) -> str:
    token = f"synthetic-sdk-{name}"
    path = tmp_path / "browser-clients.json"
    document = json.loads(path.read_text())
    document["clients"].append(
        {
            "client_id": name,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "scopes": ["read"],
            "tenant_id": "tenant-a",
            "household_ids": ["home-a"],
            "roles": ["service"],
            **authority,
        }
    )
    path.write_text(json.dumps(document))
    return token


async def test_sdk_http_scope_matrix_never_expands_empty_capability_selection(
    tmp_path, monkeypatch
):
    async with browser_household(tmp_path) as household:
        canonical = await _seed_acl_state(household.gateway.runtime)
        calls = []
        original = StateService.get_with_diagnostics

        async def observed(self, *args, **kwargs):
            calls.append((args, kwargs))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(StateService, "get_with_diagnostics", observed)
        endpoint = household.base_url + household.gateway.runtime.settings.mcp_path
        cases = [
            (
                "bounded",
                {
                    "area_ids": ["allowed"],
                    "device_ids": ["fixture.allowed"],
                    "capabilities": ["brightness"],
                },
                True,
                {"brightness"},
            ),
            (
                "empty-acls",
                {"area_ids": [], "device_ids": [], "capabilities": []},
                True,
                {"brightness", "temperature"},
            ),
            ("tenant", {"tenant_id": "tenant-b"}, False, set()),
            ("home", {"household_ids": ["home-b"]}, False, set()),
            ("area", {"area_ids": ["foreign"]}, False, set()),
            ("device", {"device_ids": ["fixture.foreign"]}, False, set()),
            ("undeclared", {"capabilities": ["not_declared"]}, True, set()),
        ]
        for name, authority, allowed, expected in cases:
            token = _add_reader(tmp_path, name, **authority)
            household.gateway.server._token_verifier.reload()
            async with _sdk(endpoint, token) as session:
                for selector in (None, []):
                    calls.clear()
                    arguments = {"devices": ["fixture.allowed"], "allow_stale": False}
                    if selector is not None:
                        arguments["capabilities"] = selector
                    result = await _tool(session, "get_state", arguments)
                    if allowed:
                        assert {state["capability"] for state in result["states"]} == expected
                        assert len(calls) == int(bool(expected))
                    else:
                        assert result["error"]["code"] == "insufficient_scope"
                        assert calls == []
                if name == "bounded":
                    for resource in ("devices", "capabilities", "coverage", "coverage-evidence"):
                        payload = json.dumps(await _resource(session, resource))
                        assert "temperature" not in payload
                        assert "brightness" in payload
                        assert "fixture.foreign" not in payload
                        assert "fixture.unassigned" not in payload
                    for arguments in (
                        {"devices": ["fixture.allowed"], "capabilities": ["temperature"]},
                        {"devices": ["fixture.foreign"]},
                        {"devices": ["fixture.unassigned"]},
                    ):
                        calls.clear()
                        denied = await _tool(session, "get_state", arguments)
                        assert denied["error"]["code"] == "insufficient_scope"
                        assert calls == []
        assert [
            device.model_dump(mode="json") for device in household.gateway.runtime.registry.devices
        ] == canonical
        assert household.source.reads == household.source.commands == 0


@pytest.mark.parametrize(
    "foreign_scope",
    [
        LeaseScope(tenant_id="tenant-a", household_id="home-b", deployment_id="edge"),
        LeaseScope(tenant_id="tenant-b", household_id="home-a", deployment_id="edge"),
    ],
)
async def test_real_http_foreign_connection_uuid_is_rejected_before_state(
    tmp_path, monkeypatch, foreign_scope
):
    async with browser_household(tmp_path) as household:
        runtime = household.gateway.runtime
        await _seed_acl_state(runtime)
        own = _profile(str(uuid4()), unique_id="own-profile").model_copy(
            update={"credential_ref": None, "enabled": False}
        )
        foreign = _profile(str(uuid4()), unique_id="private-foreign-profile").model_copy(
            update={"credential_ref": None, "enabled": False}
        )
        await runtime.connection_manager.repository.save(own)
        foreign_repository = ConnectionRepository(runtime.database, scope=foreign_scope)
        await foreign_repository.save(foreign)
        calls = []

        async def forbidden(*args, **kwargs):
            calls.append("state")
            raise AssertionError("foreign connection reached state service")

        monkeypatch.setattr(StateService, "get_with_diagnostics", forbidden)
        async with httpx.AsyncClient(
            base_url=household.base_url,
            headers={"Authorization": f"Bearer {household.read_token}"},
        ) as client:
            profiles = await client.get("/api/v1/connections")
            response = await client.get(
                "/api/v1/onboarding/state/fixture.allowed",
                params={
                    "connection_id": str(foreign.id),
                    "capability": "brightness",
                },
            )
        assert profiles.status_code == 200
        assert str(own.id) in profiles.text and str(foreign.id) not in profiles.text
        assert response.status_code == 404
        assert str(foreign.id) not in response.text and foreign.unique_id not in response.text
        assert calls == []
        assert await foreign_repository.get(foreign.id) == foreign
        assert household.source.reads == household.source.commands == 0
