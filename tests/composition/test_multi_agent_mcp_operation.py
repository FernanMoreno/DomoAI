"""Composition proof for four MCP clients over one configured runtime."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.config.settings import Settings
from domoai.domain.errors import DomainError
from domoai.domain.models import Command, Plan, PlanStatus
from domoai.mcp.gateway import build_gateway
from domoai.mcp.probe import digest_json
from domoai.persistence.repositories import ExecutionOutcomeRepository, PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def _write_tokens(path: Path) -> dict[str, str]:
    tokens = {
        "codex": "composition-codex-secret",
        "claude": "composition-claude-secret",
        "opencode": "composition-opencode-secret",
        "gemini": "composition-gemini-secret",
    }
    path.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": client_id,
                        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                        "scopes": ["read"],
                    }
                    for client_id, token in tokens.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    return tokens


async def _session_evidence(
    app: Any,
    endpoint: str,
    token: str,
) -> dict[str, str]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=endpoint.removesuffix("/mcp"),
        headers={"Authorization": f"Bearer {token}"},
    ) as http_client:
        async with streamable_http_client(endpoint, http_client=http_client) as (
            read_stream,
            write_stream,
            _,
        ):
            try:
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    runtime_result = await session.read_resource(
                        cast(AnyUrl, "domotics://runtime")
                    )
                    discovery_result = await session.call_tool(
                        "discover_devices", {"refresh": False}
                    )
                    runtime_text = getattr(runtime_result.contents[0], "text", None)
                    assert isinstance(runtime_text, str)
                    runtime = json.loads(runtime_text)
                    discovery = discovery_result.structuredContent
                    assert isinstance(discovery, dict)
                    assert runtime["runtime_revision"] == discovery["runtime_revision"]
                    return {
                        "catalog_digest": digest_json(sorted(tool.name for tool in tools.tools)),
                        "runtime_revision": runtime["runtime_revision"],
                        "registry_digest": digest_json(
                            {"devices": discovery["devices"], "areas": discovery["areas"]}
                        ),
                        "discovery_digest": digest_json(discovery),
                    }
            finally:
                await read_stream.aclose()
                await write_stream.aclose()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_four_mcp_clients_observe_one_sqlite_runtime(tmp_path: Path) -> None:
    token_file = tmp_path / "clients.json"
    tokens = _write_tokens(token_file)
    database_path = tmp_path / "shared-runtime.sqlite3"
    settings = Settings(
        database_path=database_path,
        mcp_host="127.0.0.1",
        mcp_port=8124,
        mcp_public_url="http://127.0.0.1:8124",
        mcp_client_token_file=token_file,
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    endpoint = settings.mcp_public_url + settings.mcp_path

    try:
        async with gateway.app.router.lifespan_context(gateway.app):
            evidence = await asyncio.gather(
                *(_session_evidence(gateway.app, endpoint, token) for token in tokens.values())
            )

            assert len(evidence) == 4
            first = evidence[0]
            assert all(item == first for item in evidence)
            assert gateway.runtime.settings.database_path == database_path
            assert gateway.runtime.registry is gateway.runtime.plan_service.registry
            assert gateway.runtime.state_store is gateway.runtime.plan_service.state_store
            assert gateway.runtime.facade.executor.adapter is gateway.runtime.adapter
    finally:
        await gateway.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_concurrent_duplicate_execution_has_one_sqlite_claim_and_write(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "execution.sqlite3")
    await database.initialize()
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    plan_repository = PlanRepository(database)
    outcome_repository = ExecutionOutcomeRepository(database)

    try:
        await discovery.refresh()
        plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
        executor = PlanExecutor(
            adapter,
            plan_service,
            audit,
            plan_repository=plan_repository,
            outcome_repository=outcome_repository,
        )
        light_id = next(device.id for device in registry.devices if device.type.value == "light")
        plan = plan_service.validate(
            Plan(
                id="multi-agent-duplicate-execution",
                commands=[
                    Command(
                        id="multi-agent-duplicate-command",
                        device_id=light_id,
                        command="set_brightness",
                        value=60,
                        idempotency_key="multi-agent-duplicate-intent",
                    )
                ],
            )
        )
        assert plan.status is PlanStatus.READY
        await plan_repository.save(plan)

        results = await asyncio.gather(
            executor.execute(plan), executor.execute(plan), return_exceptions=True
        )
        successes = [result for result in results if not isinstance(result, BaseException)]
        failures = [result for result in results if isinstance(result, BaseException)]

        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], DomainError)
        assert len(adapter.calls) == 1
        persisted = await plan_repository.get(plan.id)
        assert persisted is not None and persisted.status is PlanStatus.COMPLETED
        assert len(await outcome_repository.list_for_plan(plan.id)) == 1
        assert len(await outcome_repository.list_attempts_for_plan(plan.id)) == 1
    finally:
        await database.close()
