import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import anyio
import pytest
from mcp import ClientSession

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.state_service import StateService
from domoai.domain.errors import DomainError
from domoai.domain.models import Policy, PolicyAction, StateStatus
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.optimizer.energy import StaticEnergyContextProvider
from domoai.persistence.repositories import (
    AuditEventRepository,
    BundleCommitRepository,
    PlanRepository,
    RecurringScheduleRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import (
    ApprovalAssertion,
    ApprovalStore,
    OperatorPrincipal,
)
from domoai.runtime.bundle_commit import (
    BundleCommitRequestMember,
    BundleCommitService,
    bundle_approval_digest,
)
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot

OPERATOR_TOKEN = "test-operator-secret"


def structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


async def build_context() -> DomoticsMcpContext:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    return DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=[],
        energy_context_provider=StaticEnergyContextProvider(energy_context_for()),
    )


async def build_context_with_scheduler(tmp_path) -> DomoticsMcpContext:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    facade = DomoticsFacade(plan_service, executor)
    scheduled_plan_repository = ScheduledPlanRepository(database)
    scheduler = Scheduler(executor, scheduled_plan_repository, audit)
    approval_store = ApprovalStore(operator_token=OPERATOR_TOKEN, allow_legacy_token=True)
    plans: dict[str, Any] = {}
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=[],
        energy_context_provider=StaticEnergyContextProvider(energy_context_for()),
        approval_store=approval_store,
        plan_repository=plan_repository,
        plans=plans,
        scheduler=scheduler,
    )
    context.bundle_commit_service = BundleCommitService(
        facade=facade,
        plans=plans,
        approval_store=approval_store,
        bundle_repository=BundleCommitRepository(database),
        scheduled_repository=scheduled_plan_repository,
        audit=audit,
        plan_repository=plan_repository,
    )
    return context


async def build_context_with_audit_repository(tmp_path) -> DomoticsMcpContext:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    audit_repository = AuditEventRepository(database)
    audit = AuditLog(sink=audit_repository)
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    return DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=[],
        energy_context_provider=StaticEnergyContextProvider(energy_context_for()),
        audit_repository=audit_repository,
    )


async def build_composed_context() -> DomoticsMcpContext:
    home_assistant = RecordingAdapter(
        "home_assistant", source_snapshot(adapter_id="home_assistant")
    )
    native = RecordingAdapter(
        "modbus", source_snapshot(adapter_id="modbus", include_shared_device=False)
    )
    registry = DeviceRegistry()
    adapter = CompositeAdapter([home_assistant, native], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await adapter.connect()
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    return DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=[],
    )


@pytest.mark.asyncio
async def test_mcp_v1_exposes_stable_semantic_surface() -> None:
    server = create_domotics_server(await build_context())

    tools = [tool.name for tool in await server.list_tools()]
    resources = [str(resource.uri) for resource in await server.list_resources()]

    assert tools == [
        "discover_devices",
        "get_state",
        "get_energy_context",
        "validate_command",
        "validate_plan",
        "request_approval",
        "execute_plan",
        "schedule_plan",
        "cancel_scheduled_plan",
        "reschedule_plan",
        "list_scheduled_plans",
        "schedule_recurring_plan",
        "cancel_recurring_schedule",
        "list_recurring_schedules",
        "list_audit_events",
    ]
    assert resources == [
        "domotics://areas",
        "domotics://capabilities",
        "domotics://devices",
        "domotics://energy",
        "domotics://policies",
        "domotics://metrics",
    ]


@pytest.mark.asyncio
async def test_invalid_mcp_command_returns_safe_error_envelope_without_adapter_call() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    result = structured(
        await server.call_tool(
            "validate_command",
            {
                "command": {
                    "device_id": device_id,
                    "command": "set_brightness",
                    "value": 140,
                    "idempotency_key": "invalid-intent",
                }
            },
        )
    )

    assert result["error"]["code"] == "validation_error"
    assert adapter.calls == []
    assert "token" not in str(result).lower()


@pytest.mark.asyncio
async def test_validate_command_rejects_explicit_unit_mismatch_before_adapter_call() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    climate_id = next(
        device.id for device in context.registry.devices if device.type.value == "climate"
    )

    result = structured(
        await server.call_tool(
            "validate_command",
            {
                "command": {
                    "id": "command-contract-unit-mismatch",
                    "device_id": climate_id,
                    "command": "set_temperature",
                    "value": 22,
                    "unit": "°F",
                    "idempotency_key": "intent-contract-unit-mismatch",
                }
            },
        )
    )

    assert result["validation"]["status"] == "invalid"
    assert any(
        error["code"] == "invalid_capability" and error["field"] == "unit"
        for error in result["validation"]["errors"]
    )
    assert cast(SimulatedHomeAdapter, context.facade.executor.adapter).calls == []


@pytest.mark.asyncio
async def test_validate_command_returns_canonical_unit() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    climate_id = next(
        device.id for device in context.registry.devices if device.type.value == "climate"
    )

    result = structured(
        await server.call_tool(
            "validate_command",
            {
                "command": {
                    "id": "command-contract-unit-default",
                    "device_id": climate_id,
                    "command": "set_temperature",
                    "value": 22,
                    "idempotency_key": "intent-contract-unit-default",
                }
            },
        )
    )

    assert result["validation"]["status"] == "valid"
    assert result["command"]["unit"] == "°C"


@pytest.mark.asyncio
async def test_validate_plan_generates_agent_request_id_when_omitted() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    result = structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "plan-contract-no-request-id",
                    "commands": [
                        {
                            "id": "cmd-contract-no-request-id",
                            "device_id": device_id,
                            "command": "turn_on",
                            "idempotency_key": "intent-contract-no-request-id",
                        }
                    ],
                }
            },
        )
    )

    assert result["plan"]["agent_request_id"]


@pytest.mark.asyncio
async def test_validate_plan_preserves_a_supplied_agent_request_id() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    result = structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "plan-contract-explicit-request-id",
                    "commands": [
                        {
                            "id": "cmd-contract-explicit-request-id",
                            "device_id": device_id,
                            "command": "turn_on",
                            "idempotency_key": "intent-contract-explicit-request-id",
                        }
                    ],
                    "agent_request_id": "agent-req-contract-explicit",
                }
            },
        )
    )

    assert result["plan"]["agent_request_id"] == "agent-req-contract-explicit"


@pytest.mark.asyncio
async def test_terminal_plan_cannot_be_revalidated_into_executable_state(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    plan_input = {
        "id": "plan-terminal-revalidation-contract",
        "commands": [
            {
                "id": "cmd-terminal-revalidation-contract",
                "device_id": device_id,
                "command": "turn_on",
                "idempotency_key": "intent-terminal-revalidation-contract",
            }
        ],
    }

    validated = structured(await server.call_tool("validate_plan", {"plan": plan_input}))
    executed = structured(
        await server.call_tool(
            "execute_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
            },
        )
    )
    assert "outcomes" in executed
    assert len(adapter.calls) == 1

    persisted = await context.plan_repository.get(validated["plan"]["id"])
    assert persisted is not None
    assert persisted.status.value == "completed"

    rejected = structured(
        await server.call_tool("validate_plan", {"plan": persisted.model_dump(mode="json")})
    )

    assert rejected["error"]["code"] == "invalid_transition"
    assert context.plans[validated["plan"]["id"]].status.value == "completed"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_plan_identity_conflict_rejects_changed_command(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    plan_id = "plan-identity-conflict-contract"
    first = structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": plan_id,
                    "commands": [
                        {
                            "id": "cmd-identity-conflict-contract",
                            "device_id": device_id,
                            "command": "turn_on",
                            "idempotency_key": "intent-identity-conflict-contract",
                        }
                    ],
                }
            },
        )
    )
    assert "plan" in first

    changed = structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": plan_id,
                    "commands": [
                        {
                            "id": "cmd-identity-conflict-contract",
                            "device_id": device_id,
                            "command": "turn_off",
                            "idempotency_key": "intent-identity-conflict-contract",
                        }
                    ],
                }
            },
        )
    )

    assert changed["error"]["code"] == "plan_identity_conflict"


@pytest.mark.asyncio
async def test_mcp_execution_rejects_matching_stale_precondition_without_write(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    switch_id = next(
        device.id for device in context.registry.devices if device.type.value == "switch"
    )
    light_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    snapshot = await context.discovery.state_store.get(switch_id, "power")
    assert snapshot is not None
    await context.discovery.state_store.save(
        snapshot.model_copy(update={"status": StateStatus.STALE})
    )

    validated = structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "plan-mcp-stale-precondition",
                    "commands": [
                        {
                            "id": "command-mcp-stale-precondition",
                            "device_id": light_id,
                            "command": "set_brightness",
                            "value": 60,
                            "unit": "%",
                            "idempotency_key": "intent-mcp-stale-precondition",
                            "preconditions": [
                                {
                                    "device_id": switch_id,
                                    "capability": "power",
                                    "expected": snapshot.value,
                                }
                            ],
                        }
                    ],
                }
            },
        )
    )
    executed = structured(
        await server.call_tool(
            "execute_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
            },
        )
    )

    assert executed["outcomes"][0]["status"] == "rejected"
    assert (
        executed["outcomes"][0]["error"]["details"]["preconditions"][0]["freshness"]["status"]
        == "stale"
    )
    assert (
        executed["outcomes"][0]["error"]["details"]["preconditions"][0]["freshness"][
            "source_revision"
        ]
        > 0
    )
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_validate_command_accepts_and_defaults_agent_request_id() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )

    with_explicit = structured(
        await server.call_tool(
            "validate_command",
            {
                "command": {
                    "id": "cmd-contract-command-explicit",
                    "device_id": device_id,
                    "command": "turn_on",
                    "idempotency_key": "intent-contract-command-explicit",
                },
                "agent_request_id": "agent-req-contract-command",
            },
        )
    )
    without_explicit = structured(
        await server.call_tool(
            "validate_command",
            {
                "command": {
                    "id": "cmd-contract-command-default",
                    "device_id": device_id,
                    "command": "turn_on",
                    "idempotency_key": "intent-contract-command-default",
                }
            },
        )
    )

    assert context.plans[with_explicit["plan_id"]].agent_request_id == "agent-req-contract-command"
    assert context.plans[without_explicit["plan_id"]].agent_request_id


@pytest.mark.asyncio
async def test_metrics_resource_reports_unavailable_without_a_collector() -> None:
    server = create_domotics_server(await build_context())

    contents = list(await server.read_resource("domotics://metrics"))

    body = json.loads(contents[0].content)
    assert body["available"] is False


@pytest.mark.asyncio
async def test_resource_snapshots_are_json_and_versioned() -> None:
    server = create_domotics_server(await build_context())

    contents = list(await server.read_resource("domotics://devices"))

    assert len(contents) == 1
    assert '"schema_version":"v1"' in contents[0].content


@pytest.mark.asyncio
async def test_list_audit_events_reports_unavailable_without_a_repository() -> None:
    server = create_domotics_server(await build_context())

    result = structured(await server.call_tool("list_audit_events", {}))

    assert "error" in result
    assert "events" not in result


@pytest.mark.asyncio
async def test_list_audit_events_filters_through_to_the_repository(tmp_path) -> None:
    context = await build_context_with_audit_repository(tmp_path)
    server = create_domotics_server(context)
    assert context.audit_repository is not None
    await context.audit_repository.append(
        event_id="e1",
        event_type="plan_approved",
        actor="system",
        subject_id="plan-1",
        payload={},
        created_at=datetime.now(UTC).isoformat(),
    )
    await context.audit_repository.append(
        event_id="e2",
        event_type="precondition_failed",
        actor="system",
        subject_id="plan-2",
        payload={},
        created_at=(datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
    )

    all_events = structured(await server.call_tool("list_audit_events", {}))
    filtered = structured(
        await server.call_tool("list_audit_events", {"event_type": "precondition_failed"})
    )

    all_ids = {event["id"] for event in all_events["events"]}
    assert {"e1", "e2"} <= all_ids
    assert [event["id"] for event in filtered["events"]] == ["e2"]


@pytest.mark.asyncio
async def test_list_audit_events_rejects_naive_since(tmp_path) -> None:
    context = await build_context_with_audit_repository(tmp_path)
    server = create_domotics_server(context)

    result = structured(
        await server.call_tool(
            "list_audit_events",
            {"since": "2026-08-21T10:00:00"},
        )
    )

    assert result["error"]["code"] == "validation_error"
    assert "events" not in result


@pytest.mark.asyncio
async def test_composed_runtime_keeps_mcp_surface_semantic_and_aggregated() -> None:
    context = await build_composed_context()
    server = create_domotics_server(context)

    tools = [tool.name for tool in await server.list_tools()]
    inventory = structured(await server.call_tool("discover_devices", {"refresh": False}))
    validation = structured(
        await server.call_tool(
            "validate_command",
            {
                "command": {
                    "id": "composed-mcp-command",
                    "device_id": "living_room.main_light",
                    "command": "turn_on",
                    "idempotency_key": "composed-mcp-key",
                }
            },
        )
    )

    assert tools == [
        "discover_devices",
        "get_state",
        "get_energy_context",
        "validate_command",
        "validate_plan",
        "request_approval",
        "execute_plan",
        "schedule_plan",
        "cancel_scheduled_plan",
        "reschedule_plan",
        "list_scheduled_plans",
        "schedule_recurring_plan",
        "cancel_recurring_schedule",
        "list_recurring_schedules",
        "list_audit_events",
    ]
    assert {device["id"] for device in inventory["devices"]} >= {
        "living_room.main_light",
        "home_assistant.environment",
        "modbus.environment",
    }
    assert validation["validation"]["status"] == "valid"
    assert "register" not in str(inventory).lower()


@pytest.mark.asyncio
async def test_mcp_energy_context_is_typed_read_only_and_horizon_bounded() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    requested = energy_context_for().horizon

    result = structured(
        await server.call_tool(
            "get_energy_context",
            {"horizon": requested.model_dump(mode="json")},
        )
    )

    assert result["schema_version"] == "v1"
    assert result["context"]["horizon"]["resolution_minutes"] == 15
    assert "protocol" not in str(result["context"]).lower()


async def call_with_in_process_client(context: DomoticsMcpContext) -> tuple[list[str], str]:
    server = create_domotics_server(context)
    client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream(0)
    server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream(0)

    async def run_server() -> None:
        await server._mcp_server.run(
            client_to_server_receive,
            server_to_client_send,
            server._mcp_server.create_initialization_options(),
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_server)
        async with ClientSession(server_to_client_receive, client_to_server_send) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("discover_devices", {"refresh": False})
        task_group.cancel_scope.cancel()

    return [tool.name for tool in tools.tools], json.dumps(
        result.structuredContent, sort_keys=True, default=str
    )


@pytest.mark.asyncio
async def test_two_mcp_clients_receive_equivalent_contract_results() -> None:
    first_tools, first_result = await call_with_in_process_client(await build_context())
    second_tools, second_result = await call_with_in_process_client(await build_context())

    assert first_tools == second_tools
    assert first_result == second_result


async def build_confirmation_required_context() -> DomoticsMcpContext:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    policies = [
        Policy(
            id="confirm-brightness",
            target={"capability": "brightness"},
            action=PolicyAction.CONFIRM,
        )
    ]
    plan_service = PlanService(registry, state_store, PolicyEngine(policies), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    return DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=policies,
        approval_store=ApprovalStore(operator_token=OPERATOR_TOKEN, allow_legacy_token=True),
    )


async def _validated_plan_requiring_confirmation(
    server: Any, context: DomoticsMcpContext
) -> dict[str, Any]:
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    validated = structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "plan-confirm-1",
                    "commands": [
                        {
                            "id": "command-confirm-1",
                            "device_id": device_id,
                            "command": "set_brightness",
                            "value": 50,
                            "idempotency_key": "confirm-intent-1",
                        }
                    ],
                },
                "mode": "preview",
            },
        )
    )
    assert validated["validation"]["status"] == "requires_confirmation"
    return validated


@pytest.mark.asyncio
async def test_execute_plan_rejects_a_caller_fabricated_approval_object() -> None:
    context = await build_confirmation_required_context()
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)

    result = structured(
        await server.call_tool(
            "execute_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approval_id": "caller-fabricated-approval",
            },
        )
    )

    assert result["error"]["code"] == "approval_required"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_request_approval_refuses_when_no_operator_token_configured() -> None:
    context = await build_confirmation_required_context()
    context.approval_store = ApprovalStore()
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)

    for candidate_token in ("", "guess-1", OPERATOR_TOKEN):
        result = structured(
            await server.call_tool(
                "request_approval",
                {
                    "plan_id": validated["plan"]["id"],
                    "validation_digest": validated["validation"]["digest"],
                    "approved_by": "operator",
                    "operator_token": candidate_token,
                },
            )
        )
        assert result["error"]["code"] == "operator_authentication_failed"


@pytest.mark.asyncio
async def test_request_approval_rejects_agent_self_approval_without_operator_token() -> None:
    context = await build_confirmation_required_context()
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)

    missing_token = structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approved_by": "an-agent-choosing-its-own-label",
                "operator_token": "",
            },
        )
    )
    assert missing_token["error"]["code"] == "operator_authentication_failed"

    wrong_token = structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approved_by": "an-agent-choosing-its-own-label",
                "operator_token": "guessed-token",
            },
        )
    )
    assert wrong_token["error"]["code"] == "operator_authentication_failed"

    # Neither attempt produced a usable approval_id, so execute_plan still
    # has nothing to consume, exactly like a caller-fabricated approval.
    result = structured(
        await server.call_tool(
            "execute_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approval_id": "caller-fabricated-approval",
            },
        )
    )
    assert result["error"]["code"] == "approval_required"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_request_approval_uses_server_principal_not_caller_identity() -> None:
    context = await build_confirmation_required_context()
    principal = OperatorPrincipal(
        id="human-42", authentication_context="oidc", session_id="session-7"
    )
    context.operator_principal_provider = lambda: principal
    context.approval_store = ApprovalStore(operator_token=OPERATOR_TOKEN, allow_legacy_token=False)
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)
    assertion_now = datetime.now(UTC)
    context.operator_approval_assertion_provider = (
        lambda plan_id, validation_digest, bundle_digest: ApprovalAssertion(
            principal=principal,
            plan_id=plan_id,
            validation_digest=validation_digest,
            bundle_digest=bundle_digest,
            nonce="contract-human-gesture-1",
            approved_at=assertion_now,
            expires_at=assertion_now + timedelta(minutes=5),
        )
    )

    approval = structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approved_by": "caller-forged-name",
                "operator_token": "caller-forged-token",
            },
        )
    )
    grant = context.approval_store.consume(
        approval["approval_id"], context.plans[validated["plan"]["id"]]
    )
    assert grant.approved_by == "human-42"
    assert grant.authentication_context == "oidc"
    assert grant.session_id == "session-7"


@pytest.mark.asyncio
async def test_legacy_mcp_approval_requires_explicit_compatibility_mode() -> None:
    context = await build_confirmation_required_context()
    context.approval_store = ApprovalStore(operator_token=OPERATOR_TOKEN, allow_legacy_token=False)
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)

    result = structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approved_by": "operator",
                "operator_token": OPERATOR_TOKEN,
            },
        )
    )
    assert result["error"]["code"] == "operator_authentication_failed"


@pytest.mark.asyncio
async def test_execute_plan_succeeds_after_request_approval() -> None:
    context = await build_confirmation_required_context()
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)

    approval = structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approved_by": "operator",
                "operator_token": OPERATOR_TOKEN,
            },
        )
    )
    assert "approval_id" in approval

    executed = structured(
        await server.call_tool(
            "execute_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approval_id": approval["approval_id"],
            },
        )
    )
    assert executed["outcomes"][0]["status"] == "confirmed_success"


@pytest.mark.asyncio
async def test_approval_id_is_rejected_once_already_consumed() -> None:
    context = await build_confirmation_required_context()
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)

    approval = structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approved_by": "operator",
                "operator_token": OPERATOR_TOKEN,
            },
        )
    )
    plan = context.plans[validated["plan"]["id"]]

    # Directly exercises single-use enforcement at the authoritative store,
    # independent of the plan lifecycle mutation that happens on a real
    # execute_plan call (tracked separately as a lifecycle/idempotency gap).
    context.approval_store.consume(approval["approval_id"], plan)
    with pytest.raises(DomainError):
        context.approval_store.consume(approval["approval_id"], plan)


async def _validated_safe_plan(
    server, context: DomoticsMcpContext, *, execute_at: str | None = None
) -> dict[str, Any]:
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    return structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "plan-schedulable-1",
                    **({"execute_at": execute_at} if execute_at is not None else {}),
                    "commands": [
                        {
                            "id": "command-schedulable-1",
                            "device_id": device_id,
                            "command": "set_brightness",
                            "value": 60,
                            "unit": "%",
                            "idempotency_key": "intent-schedulable-1",
                        }
                    ],
                }
            },
        )
    )


@pytest.mark.asyncio
async def test_schedule_plan_defers_execution_until_due(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    validated = await _validated_safe_plan(server, context, execute_at=future)

    scheduled = structured(
        await server.call_tool(
            "schedule_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "execute_at": future,
            },
        )
    )
    assert scheduled["plan_id"] == validated["plan"]["id"]

    pending = structured(await server.call_tool("list_scheduled_plans", {}))
    assert [item["plan_id"] for item in pending["plans"]] == [validated["plan"]["id"]]

    direct_execution = structured(
        await server.call_tool(
            "execute_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
            },
        )
    )
    assert direct_execution["error"]["code"] == "not_yet_due"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_validated_plan_survives_mcp_runtime_restart(tmp_path) -> None:
    first_context = await build_context_with_scheduler(tmp_path)
    first_server = create_domotics_server(first_context)
    validated = await _validated_safe_plan(first_server, first_context)

    second_context = await build_context_with_scheduler(tmp_path)
    second_server = create_domotics_server(second_context)
    result = structured(
        await second_server.call_tool(
            "execute_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
            },
        )
    )

    assert result["plan_id"] == validated["plan"]["id"]
    assert result["outcomes"]
    assert cast(SimulatedHomeAdapter, second_context.facade.executor.adapter).calls


@pytest.mark.asyncio
async def test_schedule_plan_rejects_naive_execute_at(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    validated = await _validated_safe_plan(server, context)

    result = structured(
        await server.call_tool(
            "schedule_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "execute_at": "2026-08-22T03:00:00",
            },
        )
    )

    assert result["error"]["code"] == "validation_error"
    assert await context.scheduler.list_pending() == []


@pytest.mark.asyncio
async def test_bundle_commit_tool_returns_durable_aggregate_and_is_idempotent(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    tools = [tool.name for tool in await server.list_tools()]
    assert "commit_or_schedule_bundle" in tools
    validated = await _validated_safe_plan(server, context)
    member = BundleCommitRequestMember(
        plan_id=validated["plan"]["id"],
        validation_digest=validated["validation"]["digest"],
    )
    digest = bundle_approval_digest("contract-bundle-1", [member])
    arguments = {
        "bundle_digest": digest,
        "scenario_id": "contract-bundle-1",
        "members": [member.model_dump(mode="json")],
    }

    first = structured(await server.call_tool("commit_or_schedule_bundle", arguments))
    second = structured(await server.call_tool("commit_or_schedule_bundle", arguments))

    assert first["status"] == "completed"
    assert first["bundle_commit_id"] == second["bundle_commit_id"]
    assert first["members"] == [
        {
            "plan_id": member.plan_id,
            "status": "executed",
            "execution_status": "confirmed_success",
            "error_code": None,
        }
    ]
    assert len(cast(SimulatedHomeAdapter, context.facade.executor.adapter).calls) == 1


@pytest.mark.asyncio
async def test_cancel_scheduled_plan_removes_it_from_pending(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    validated = await _validated_safe_plan(server, context, execute_at=future)
    await server.call_tool(
        "schedule_plan",
        {
            "plan_id": validated["plan"]["id"],
            "validation_digest": validated["validation"]["digest"],
            "execute_at": future,
        },
    )

    cancelled = structured(
        await server.call_tool("cancel_scheduled_plan", {"plan_id": validated["plan"]["id"]})
    )
    assert cancelled["cancelled"] is True

    pending = structured(await server.call_tool("list_scheduled_plans", {}))
    assert pending["plans"] == []

    second_attempt = structured(
        await server.call_tool("cancel_scheduled_plan", {"plan_id": validated["plan"]["id"]})
    )
    assert second_attempt["cancelled"] is False
    persisted = await context.plan_repository.get(validated["plan"]["id"])
    assert persisted is not None
    assert persisted.status.value == "cancelled"


@pytest.mark.asyncio
async def test_reschedule_plan_changes_pending_execute_at(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    first_time = datetime.now(UTC) + timedelta(hours=1)
    validated = await _validated_safe_plan(server, context, execute_at=first_time.isoformat())
    await server.call_tool(
        "schedule_plan",
        {
            "plan_id": validated["plan"]["id"],
            "validation_digest": validated["validation"]["digest"],
            "execute_at": first_time.isoformat(),
        },
    )
    new_time = datetime.now(UTC) + timedelta(hours=2)

    rescheduled = structured(
        await server.call_tool(
            "reschedule_plan",
            {"plan_id": validated["plan"]["id"], "execute_at": new_time.isoformat()},
        )
    )
    assert rescheduled["error"]["code"] == "reschedule_requires_revalidation"

    pending = structured(await server.call_tool("list_scheduled_plans", {}))
    assert pending["plans"][0]["execute_at"] == first_time.isoformat()
    persisted = await context.plan_repository.get(validated["plan"]["id"])
    assert persisted is not None
    assert persisted.execute_at == first_time


@pytest.mark.asyncio
async def test_reschedule_plan_rejects_naive_execute_at_and_preserves_schedule(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    original_time = datetime.now(UTC) + timedelta(hours=1)
    validated = await _validated_safe_plan(server, context, execute_at=original_time.isoformat())
    await server.call_tool(
        "schedule_plan",
        {
            "plan_id": validated["plan"]["id"],
            "validation_digest": validated["validation"]["digest"],
            "execute_at": original_time.isoformat(),
        },
    )

    result = structured(
        await server.call_tool(
            "reschedule_plan",
            {"plan_id": validated["plan"]["id"], "execute_at": "2026-08-22T04:00:00"},
        )
    )

    assert result["error"]["code"] == "validation_error"
    pending = structured(await server.call_tool("list_scheduled_plans", {}))
    assert pending["plans"][0]["execute_at"] == original_time.isoformat()


@pytest.mark.asyncio
async def test_schedule_plan_requires_existing_validation() -> None:
    context = await build_context()
    server = create_domotics_server(context)

    result = structured(
        await server.call_tool(
            "schedule_plan",
            {
                "plan_id": "unknown-plan",
                "validation_digest": "sha256:unknown",
                "execute_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    )

    assert "error" in result


async def build_context_with_scheduler_and_recurring(
    tmp_path, *, require_confirmation: bool
) -> DomoticsMcpContext:
    """Same authority posture as build_confirmation_required_context, plus a
    real durable Scheduler with recurring-schedule support -- needed to
    exercise the schedule_recurring_plan approval gate (spec 145)."""

    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    policies = (
        [
            Policy(
                id="confirm-brightness",
                target={"capability": "brightness"},
                action=PolicyAction.CONFIRM,
            )
        ]
        if require_confirmation
        else []
    )
    plan_service = PlanService(registry, state_store, PolicyEngine(policies), audit)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    facade = DomoticsFacade(plan_service, executor)
    scheduled_plan_repository = ScheduledPlanRepository(database)
    recurring_repository = RecurringScheduleRepository(database)
    scheduler = Scheduler(
        executor,
        scheduled_plan_repository,
        audit,
        recurring_repository=recurring_repository,
    )
    return DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=policies,
        approval_store=ApprovalStore(operator_token=OPERATOR_TOKEN, allow_legacy_token=True),
        plan_repository=plan_repository,
        scheduler=scheduler,
    )


@pytest.mark.asyncio
async def test_schedule_recurring_plan_rejects_confirmation_required_plan_without_approval(
    tmp_path,
) -> None:
    context = await build_context_with_scheduler_and_recurring(tmp_path, require_confirmation=True)
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)

    result = structured(
        await server.call_tool(
            "schedule_recurring_plan",
            {
                "plan_id": validated["plan"]["id"],
                "time_of_day": "00:00",
                "timezone": "UTC",
            },
        )
    )

    assert result["error"]["code"] == "approval_required"
    active = structured(await server.call_tool("list_recurring_schedules", {}))
    assert active["schedules"] == []


@pytest.mark.asyncio
async def test_schedule_recurring_plan_rejects_a_caller_fabricated_approval_id(
    tmp_path,
) -> None:
    context = await build_context_with_scheduler_and_recurring(tmp_path, require_confirmation=True)
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)

    result = structured(
        await server.call_tool(
            "schedule_recurring_plan",
            {
                "plan_id": validated["plan"]["id"],
                "time_of_day": "00:00",
                "timezone": "UTC",
                "approval_id": "caller-fabricated-approval",
            },
        )
    )

    assert "error" in result
    active = structured(await server.call_tool("list_recurring_schedules", {}))
    assert active["schedules"] == []


@pytest.mark.asyncio
async def test_schedule_recurring_plan_succeeds_after_request_approval(tmp_path) -> None:
    context = await build_context_with_scheduler_and_recurring(tmp_path, require_confirmation=True)
    server = create_domotics_server(context)
    validated = await _validated_plan_requiring_confirmation(server, context)

    approval = structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approved_by": "operator",
                "operator_token": OPERATOR_TOKEN,
            },
        )
    )
    assert "approval_id" in approval

    scheduled = structured(
        await server.call_tool(
            "schedule_recurring_plan",
            {
                "plan_id": validated["plan"]["id"],
                "time_of_day": "00:00",
                "timezone": "UTC",
                "approval_id": approval["approval_id"],
            },
        )
    )

    assert "schedule_id" in scheduled
    active = structured(await server.call_tool("list_recurring_schedules", {}))
    assert len(active["schedules"]) == 1

    # The approval grant is single-use: it was consumed by creating the
    # recurring schedule and cannot also authorize a one-shot execution.
    reuse = structured(
        await server.call_tool(
            "execute_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "approval_id": approval["approval_id"],
            },
        )
    )
    assert "error" in reuse


@pytest.mark.asyncio
async def test_schedule_recurring_plan_does_not_require_approval_for_safe_plan(
    tmp_path,
) -> None:
    context = await build_context_with_scheduler_and_recurring(
        tmp_path, require_confirmation=False
    )
    server = create_domotics_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    validated = structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "plan-safe-recurring",
                    "commands": [
                        {
                            "id": "command-safe-recurring",
                            "device_id": device_id,
                            "command": "turn_on",
                            "idempotency_key": "safe-recurring-intent",
                        }
                    ],
                },
                "mode": "preview",
            },
        )
    )
    assert validated["validation"]["status"] == "valid"

    scheduled = structured(
        await server.call_tool(
            "schedule_recurring_plan",
            {
                "plan_id": validated["plan"]["id"],
                "time_of_day": "00:00",
                "timezone": "UTC",
            },
        )
    )

    assert "schedule_id" in scheduled
