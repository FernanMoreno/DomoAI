from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import KnxMappingDocument
from domoai.adapters.knx.transport import InMemoryKnxTransport, KnxGroupValue
from domoai.application.discovery_service import DiscoveryService
from domoai.application.dynamic_safety import DynamicSafetyGuard
from domoai.application.execution_admission import ExecutionAdmission
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.scheduler import Scheduler
from domoai.application.state_service import StateService
from domoai.domain.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatteryControlPolicy,
    BatteryProfile,
    BatterySocObservation,
    DispatchableBatteryBinding,
)
from domoai.domain.models import Command, Policy, PolicyAction, SourceRef
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.persistence.repositories import (
    BundleCommitRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.clock import FixedClock
from domoai.runtime.control_takeover import BatteryControlCoordinator
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore

OPERATOR_TOKEN = "test-operator-secret"


def structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return cast(dict[str, Any], result[1])
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


def _binding(now: datetime) -> DispatchableBatteryBinding:
    device_id = "lab.virtual-battery"
    return DispatchableBatteryBinding(
        provider_id="knx",
        device_id=device_id,
        profile=BatteryProfile(
            capacity_kwh=10.0,
            initial_soc_kwh=5.0,
            min_soc_kwh=2.0,
            max_soc_kwh=9.0,
            max_charge_kw=4.0,
            max_discharge_kw=3.0,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
            actuator=BatteryActuator(
                device_id=device_id,
                capability="battery_control",
                charge_command="charge_battery",
                discharge_command="discharge_battery",
                stop_command="stop_battery",
                power_feedback_capability="battery.power",
                power_feedback_tolerance_kw=0.1,
                power_feedback_settle_timeout_seconds=1.0,
                power_feedback_poll_interval_seconds=0.1,
                soc_reconciliation_capability="battery.soc",
            ),
            initial_soc_observation=BatterySocObservation(
                provider_id="knx",
                device_id=device_id,
                value_kwh=5.0,
                observed_at=now,
                received_at=now,
                source_ref=SourceRef(adapter_id="knx", external_id="lab.battery"),
            ),
        ),
        capacity_evidence=BatteryCapacityEvidence(
            provider_id="knx",
            device_id=device_id,
            capacity_kwh=10.0,
            source_ref=SourceRef(adapter_id="knx", external_id="lab.battery"),
            observed_at=now,
            received_at=now,
        ),
        control_policy=BatteryControlPolicy(
            owner="domoai-lab",
            native_scheduler_status="inactive",
            allow_native_takeover=False,
            lease_seconds=60.0,
        ),
    )


async def build_context(
    tmp_path: Path,
) -> tuple[DomoticsMcpContext, KnxAdapter, InMemoryKnxTransport, FixedClock, SQLiteDatabase]:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    clock = FixedClock(now)
    incoming = [
        KnxGroupValue("4/0/1", "13.013", 5.0, now),
        KnxGroupValue("4/0/2", "9.024", 0.0, now),
        KnxGroupValue("4/0/3", "13.013", 10.0, now),
    ]
    transport = InMemoryKnxTransport(incoming=incoming, clock=clock)
    transport.write_state_map[("4/0/0", "9.024")] = ("4/0/2", "9.024")
    mapping = KnxMappingDocument.model_validate(
        {
            "schema_version": "v1",
            "entities": [
                {
                    "entity_id": "lab.battery",
                    "name": "Virtual Battery",
                    "area_id": "lab",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.power",
                            "dpt": "9.024",
                            "state_group_address": "4/0/2",
                            "command_group_address": "4/0/0",
                        },
                        {
                            "name": "battery.soc",
                            "dpt": "13.013",
                            "state_group_address": "4/0/1",
                        },
                        {
                            "name": "battery.capacity",
                            "dpt": "13.013",
                            "state_group_address": "4/0/3",
                        },
                    ],
                }
            ],
        }
    )
    adapter = KnxAdapter(transport, mapping, clock=clock)
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog(clock=clock)
    discovery = DiscoveryService(adapter, registry, state_store, audit, clock=clock)
    await adapter.connect()
    await discovery.refresh()

    binding = _binding(now)
    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [
                Policy(
                    id="confirm-battery-dispatch",
                    target={"capability": "battery.power"},
                    action=PolicyAction.CONFIRM,
                )
            ]
        ),
        audit,
        clock=clock,
        authorized_actuator_commands={
            binding.device_id: frozenset(
                {
                    binding.profile.actuator.charge_command,
                    binding.profile.actuator.discharge_command,
                    binding.profile.actuator.stop_command,
                }
            )
        },
    )
    database = SQLiteDatabase(tmp_path / "mcp-knx-e2e.sqlite3", clock=clock)
    await database.initialize()
    plan_repository = PlanRepository(database, clock=clock)
    scheduled_repository = ScheduledPlanRepository(database, clock=clock)
    bundle_repository = BundleCommitRepository(database, clock=clock)
    outcome_repository = ExecutionOutcomeRepository(database)
    approval_store = ApprovalStore(
        operator_token=OPERATOR_TOKEN,
        allow_legacy_token=True,
        clock=clock,
    )
    admission = ExecutionAdmission(
        bundle_repository=bundle_repository,
        approval_store=approval_store,
        audit=audit,
    )
    feedback_ref = SourceRef(adapter_id="knx", external_id="lab.battery")
    coordinator = BatteryControlCoordinator(
        adapter,
        binding.control_policy,
        device_id=binding.device_id,
        command_names=frozenset(
            {
                binding.profile.actuator.charge_command,
                binding.profile.actuator.discharge_command,
                binding.profile.actuator.stop_command,
            }
        ),
        stop_command=binding.profile.actuator.stop_command,
        stop_unit="kW",
        state_store=state_store,
        power_feedback_capability="battery.power",
        power_feedback_source_ref=feedback_ref,
        power_feedback_tolerance_kw=0.1,
        clock=clock,
    )
    assert await coordinator.reconcile_startup() is True
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        outcome_repository=outcome_repository,
        clock=clock,
        control_takeover=coordinator,
        execution_admission=admission,
        dynamic_safety_guard=DynamicSafetyGuard(
            state_store,
            binding.profile,
            clock=clock,
        ),
    )
    facade = DomoticsFacade(plan_service, executor)
    scheduler = Scheduler(
        executor,
        scheduled_repository,
        audit,
        bundle_repository=bundle_repository,
        execution_admission=admission,
        clock=clock,
    )
    plans: dict[str, Any] = {}
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=plan_service.policy_engine.policies,
        approval_store=approval_store,
        plan_repository=plan_repository,
        plans=plans,
        scheduler=scheduler,
    )
    return context, adapter, transport, clock, database


async def _validate_charge_plan(
    server: Any,
    *,
    plan_id: str,
    device_id: str,
    execute_at: datetime,
) -> dict[str, Any]:
    validated = structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": plan_id,
                    "execute_at": execute_at.isoformat(),
                    "commands": [
                        {
                            "id": f"{plan_id}-command",
                            "device_id": device_id,
                            "command": "charge_battery",
                            "value": 1.0,
                            "unit": "kW",
                            "idempotency_key": f"{plan_id}-once",
                            "postconditions": [
                                {
                                    "capability": "battery.power",
                                    "expected": 1.0,
                                    "tolerance": 0.1,
                                    "settle_timeout_seconds": 1.0,
                                    "poll_interval_seconds": 0.1,
                                }
                            ],
                        }
                    ],
                }
            },
        )
    )
    assert validated["validation"]["status"] == "requires_confirmation"
    return validated


@pytest.mark.asyncio
async def test_mcp_schedule_scheduler_knx_readback_and_durable_outcome(tmp_path: Path) -> None:
    context, adapter, transport, clock, database = await build_context(tmp_path)
    try:
        server = create_domotics_server(context)
        inventory = structured(await server.call_tool("discover_devices", {"refresh": False}))
        battery = next(device for device in inventory["devices"] if device["type"] == "energy")
        assert battery["id"] == "lab.virtual-battery"
        assert {
            capability["name"] for capability in battery["capabilities"]
        } >= {"battery.power", "battery.soc", "battery.capacity"}

        execute_at = clock.now() + timedelta(minutes=1)
        validated = structured(
            await server.call_tool(
                "validate_plan",
                {
                    "plan": {
                        "id": "mcp-knx-e2e-charge",
                        "execute_at": execute_at.isoformat(),
                        "commands": [
                            {
                                "id": "mcp-knx-e2e-charge-command",
                                "device_id": battery["id"],
                                "command": "charge_battery",
                                "value": 1.0,
                                "unit": "kW",
                                "idempotency_key": "mcp-knx-e2e-charge-once",
                                "postconditions": [
                                    {
                                        "capability": "battery.power",
                                        "expected": 1.0,
                                        "tolerance": 0.1,
                                        "settle_timeout_seconds": 1.0,
                                        "poll_interval_seconds": 0.1,
                                    }
                                ],
                            }
                        ],
                    }
                },
            )
        )
        assert validated["validation"]["status"] == "requires_confirmation"

        approval = structured(
            await server.call_tool(
                "request_approval",
                {
                    "plan_id": validated["plan"]["id"],
                    "validation_digest": validated["validation"]["digest"],
                    "operator_token": OPERATOR_TOKEN,
                },
            )
        )
        scheduled = structured(
            await server.call_tool(
                "schedule_plan",
                {
                    "plan_id": validated["plan"]["id"],
                    "validation_digest": validated["validation"]["digest"],
                    "execute_at": execute_at.isoformat(),
                    "approval_id": approval["approval_id"],
                },
            )
        )
        assert scheduled["plan_id"] == validated["plan"]["id"]
        assert [item.id for item in await context.scheduler.list_pending()] == [
            validated["plan"]["id"]
        ]

        clock.set(execute_at + timedelta(seconds=1))
        result = await context.scheduler.run_due()
        assert result == [{"plan_id": validated["plan"]["id"], "outcome": "executed"}]
        assert [(write.group_address, write.value) for write in transport.writes] == [
            ("4/0/0", 1.0),
            ("4/0/0", 0.0),
        ]

        persisted = await context.plan_repository.get(validated["plan"]["id"])
        assert persisted is not None
        assert persisted.status.value == "completed"
        assert persisted.execution is not None
        assert persisted.execution.outcomes[0].status.value == "confirmed_success"
        outcomes = await context.facade.executor.outcome_repository.list_for_plan(
            validated["plan"]["id"]
        )
        assert len(outcomes) == 1
        assert outcomes[0].after_state is not None
        assert outcomes[0].after_state.value == pytest.approx(1.0)

        final_readback = await adapter.read_state(
            [SourceRef(adapter_id="knx", external_id="lab.battery")]
        )
        final_power = next(
            item for item in final_readback if item.capability == "battery.power"
        )
        assert final_power.value == pytest.approx(0.0)
    finally:
        await adapter.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_mcp_schedule_rejects_missing_approval_without_knx_write(tmp_path: Path) -> None:
    context, adapter, transport, clock, database = await build_context(tmp_path)
    try:
        server = create_domotics_server(context)
        execute_at = clock.now() + timedelta(minutes=1)
        validated = await _validate_charge_plan(
            server,
            plan_id="mcp-knx-no-approval",
            device_id="lab.virtual-battery",
            execute_at=execute_at,
        )

        rejected = structured(
            await server.call_tool(
                "schedule_plan",
                {
                    "plan_id": validated["plan"]["id"],
                    "validation_digest": validated["validation"]["digest"],
                    "execute_at": execute_at.isoformat(),
                },
            )
        )

        assert rejected["error"]["code"] == "approval_required"
        assert await context.scheduler.list_pending() == []
        assert transport.writes == []
    finally:
        await adapter.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_scheduler_duplicate_delivery_does_not_replay_knx_write(tmp_path: Path) -> None:
    context, adapter, transport, clock, database = await build_context(tmp_path)
    try:
        server = create_domotics_server(context)
        execute_at = clock.now() + timedelta(minutes=1)
        validated = await _validate_charge_plan(
            server,
            plan_id="mcp-knx-duplicate-delivery",
            device_id="lab.virtual-battery",
            execute_at=execute_at,
        )
        approval = structured(
            await server.call_tool(
                "request_approval",
                {
                    "plan_id": validated["plan"]["id"],
                    "validation_digest": validated["validation"]["digest"],
                    "operator_token": OPERATOR_TOKEN,
                },
            )
        )
        await server.call_tool(
            "schedule_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "execute_at": execute_at.isoformat(),
                "approval_id": approval["approval_id"],
            },
        )

        clock.set(execute_at + timedelta(seconds=1))
        first = await context.scheduler.run_due()
        writes_after_first_delivery = list(transport.writes)
        second = await context.scheduler.run_due()

        assert first == [{"plan_id": validated["plan"]["id"], "outcome": "executed"}]
        assert second == []
        assert transport.writes == writes_after_first_delivery
        outcomes = await context.facade.executor.outcome_repository.list_for_plan(
            validated["plan"]["id"]
        )
        assert len(outcomes) == 1
    finally:
        await adapter.disconnect()
        await database.close()


@pytest.mark.asyncio
async def test_battery_cleanup_stop_confirms_zero_after_nonzero_command(tmp_path: Path) -> None:
    context, adapter, transport, _clock, database = await build_context(tmp_path)
    coordinator = context.facade.executor.control_takeover
    assert isinstance(coordinator, BatteryControlCoordinator)
    command = Command(
        id="mcp-knx-cleanup-charge",
        device_id="lab.virtual-battery",
        command="charge_battery",
        value=1.0,
        unit="kW",
        idempotency_key="mcp-knx-cleanup-charge-once",
    )
    try:
        takeover = await coordinator.acquire_for_plan(
            plan_id="mcp-knx-cleanup",
            commands=[command],
        )
        assert takeover is not None
        assert takeover.status.value == "acquired"
        acknowledgement = await adapter.execute(command)
        assert acknowledgement.accepted is True
    finally:
        released = await coordinator.release_for_plan(
            plan_id="mcp-knx-cleanup",
            execution_attempt_id="mcp-knx-cleanup-finally",
        )
        await adapter.disconnect()
        await database.close()

    assert released is True
    assert [(write.group_address, write.value) for write in transport.writes] == [
        ("4/0/0", 1.0),
        ("4/0/0", 0.0),
    ]
