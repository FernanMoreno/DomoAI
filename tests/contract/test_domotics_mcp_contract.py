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
from domoai.domain.models import Policy, PolicyAction
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.optimizer.energy import StaticEnergyContextProvider
from domoai.persistence.repositories import (
    AuditEventRepository,
    PlanRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
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
    return DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=[],
        energy_context_provider=StaticEnergyContextProvider(energy_context_for()),
        scheduler=scheduler,
    )


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
        approval_store=ApprovalStore(operator_token=OPERATOR_TOKEN),
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


async def _validated_safe_plan(server, context: DomoticsMcpContext) -> dict[str, Any]:
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    return structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "plan-schedulable-1",
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
    validated = await _validated_safe_plan(server, context)
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

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
async def test_cancel_scheduled_plan_removes_it_from_pending(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    validated = await _validated_safe_plan(server, context)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
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


@pytest.mark.asyncio
async def test_reschedule_plan_changes_pending_execute_at(tmp_path) -> None:
    context = await build_context_with_scheduler(tmp_path)
    server = create_domotics_server(context)
    validated = await _validated_safe_plan(server, context)
    first_time = datetime.now(UTC) + timedelta(hours=1)
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
    assert rescheduled["rescheduled"] is True

    pending = structured(await server.call_tool("list_scheduled_plans", {}))
    assert pending["plans"][0]["execute_at"] == new_time.isoformat()


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
