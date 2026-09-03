"""Opt-in MCP-to-KNX-to-MQTT composition against the local virtual lab."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import canonical_device_id, load_mapping
from domoai.adapters.knx.transport import XknxTransport
from domoai.application.runtime_factory import RuntimeComposition, build_runtime
from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.domain.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatteryControlPolicy,
    BatteryProfile,
    BatterySocObservation,
    DispatchableBatteryBinding,
)
from domoai.domain.models import SourceRef
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.optimizer.energy import StaticEnergyContextProvider
from domoai.persistence.repositories import ExecutionOutcomeRepository, PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from tests.fixtures.energy import energy_context_for

LIVE_OPERATOR_TOKEN = "live-mcp-knx-test-operator"


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"missing {name}")
    return value


def _structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return cast(dict[str, Any], result[1])
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


def _observed_at(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(UTC)


def _binding_from_live_snapshot(
    *,
    mapping: Any,
    snapshot: Any,
) -> DispatchableBatteryBinding:
    entity = next(entity for entity in mapping.entities if entity.semantic_type == "energy")
    device_id = canonical_device_id(entity)
    states = {state["capability"]: state for state in snapshot.source_states}
    soc_state = states["battery.soc"]
    capacity_state = states["battery.capacity"]
    soc_kwh = float(soc_state["value"])
    capacity_kwh = float(capacity_state["value"])
    assert capacity_kwh > 0
    assert 0 <= soc_kwh <= capacity_kwh

    source_ref = SourceRef(adapter_id="knx", external_id=entity.entity_id)
    observed_at = _observed_at(soc_state["observed_at"])
    return DispatchableBatteryBinding(
        provider_id="knx",
        device_id=device_id,
        profile=BatteryProfile(
            capacity_kwh=capacity_kwh,
            initial_soc_kwh=soc_kwh,
            min_soc_kwh=2.0,
            max_soc_kwh=min(9.0, capacity_kwh),
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
                power_feedback_settle_timeout_seconds=5.0,
                power_feedback_poll_interval_seconds=0.25,
                soc_reconciliation_capability="battery.soc",
            ),
            initial_soc_observation=BatterySocObservation(
                provider_id="knx",
                device_id=device_id,
                value_kwh=soc_kwh,
                observed_at=observed_at,
                received_at=observed_at,
                source_ref=source_ref,
            ),
        ),
        capacity_evidence=BatteryCapacityEvidence(
            provider_id="knx",
            device_id=device_id,
            capacity_kwh=capacity_kwh,
            source_ref=source_ref,
            observed_at=_observed_at(capacity_state["observed_at"]),
            received_at=_observed_at(capacity_state["observed_at"]),
        ),
        control_policy=BatteryControlPolicy(
            owner="domoai-live-lab",
            native_scheduler_status="inactive",
            allow_native_takeover=False,
            lease_seconds=300.0,
        ),
    )


def _context(runtime: RuntimeComposition) -> DomoticsMcpContext:
    return DomoticsMcpContext(
        discovery=runtime.discovery,
        state_service=StateService(runtime.state_store),
        facade=runtime.facade,
        registry=runtime.registry,
        policies=runtime.plan_service.policy_engine.policies,
        active_provider_ids=("knx",),
        battery_qualification=runtime.battery_qualification,
        plan_repository=runtime.plan_repository,
        approval_store=runtime.approval_store,
        plans=runtime.plans,
        scheduler=runtime.scheduler,
        audit_repository=runtime.audit_repository,
        clock=runtime.clock,
    )


@pytest.mark.asyncio
async def test_live_mcp_knx_battery_flow_persists_confirmed_outcome(tmp_path: Path) -> None:
    if os.getenv("DOMOAI_LIVE_MCP_KNX_BATTERY_ENABLE") != "1":
        pytest.skip(
            "set DOMOAI_LIVE_MCP_KNX_BATTERY_ENABLE=1 for the live MCP/KNX battery flow"
        )

    host = _required("DOMOAI_KNX_GATEWAY_HOST")
    gateway_port = int(os.getenv("DOMOAI_KNX_GATEWAY_PORT", "3672"))
    mapping_path = Path(
        os.getenv(
            "DOMOAI_KNX_BATTERY_CONFIG_PATH",
            "dev/lab/configs/knx-battery-virtual.json",
        )
    )
    mapping = load_mapping(mapping_path)
    group_dpts = {
        address: binding.dpt
        for entity in mapping.entities
        for binding in entity.capabilities
        for address in (binding.state_group_address, binding.command_group_address)
        if address is not None
    }
    transport = XknxTransport(
        host,
        gateway_port=gateway_port,
        route_back=os.getenv("DOMOAI_KNX_ROUTE_BACK", "0").lower()
        in {"1", "true", "yes", "on"},
        group_dpts=group_dpts,
    )
    adapter = KnxAdapter(transport, mapping)
    runtime: RuntimeComposition | None = None
    database_path = tmp_path / "live-mcp-knx-battery.sqlite3"
    battery_url = os.getenv("DOMOAI_BATTERY_URL", "http://127.0.0.1:8090")

    async with httpx.AsyncClient(timeout=5) as client:
        health = await client.get(f"{battery_url}/health")
        health.raise_for_status()
        assert health.json()["lab_simulation"] is True

    try:
        await adapter.connect()
        initial_snapshot = await adapter.discover()
        binding = _binding_from_live_snapshot(mapping=mapping, snapshot=initial_snapshot)
        settings = Settings(
            bootstrap_profile="none",
            database_path=database_path,
            energy_live=True,
            operator_approval_token=SecretStr(LIVE_OPERATOR_TOKEN),
            allow_legacy_operator_token=True,
            knx_gateway_host=host,
            knx_gateway_port=gateway_port,
            knx_config_path=mapping_path,
            knx_gateway_route_back=os.getenv("DOMOAI_KNX_ROUTE_BACK", "0").lower()
            in {"1", "true", "yes", "on"},
        )
        runtime = await build_runtime(
            settings,
            adapter=adapter,
            energy_context_provider=StaticEnergyContextProvider(energy_context_for()),
            dispatchable_battery_binding=binding,
            require_configured_adapter=True,
        )
        assert runtime.battery_qualification == "software-qualified"
        assert runtime.settings.battery_dispatch_production is False

        server = create_domotics_server(_context(runtime))
        inventory = _structured(
            await server.call_tool("discover_devices", {"refresh": True, "types": ["energy"]})
        )
        battery = inventory["devices"][0]
        assert battery["id"] == binding.device_id
        assert {
            capability["name"] for capability in battery["capabilities"]
        } >= {"battery.power", "battery.soc", "battery.capacity"}

        execute_at = datetime.now(UTC) + timedelta(seconds=1.5)
        validated = _structured(
            await server.call_tool(
                "validate_plan",
                {
                    "plan": {
                        "id": "live-mcp-knx-battery-charge",
                        "execute_at": execute_at.isoformat(),
                        "commands": [
                            {
                                "id": "live-mcp-knx-battery-charge-command",
                                "device_id": binding.device_id,
                                "command": "charge_battery",
                                "value": 0.5,
                                "unit": "kW",
                                "idempotency_key": "live-mcp-knx-battery-charge-once",
                                "postconditions": [
                                    {
                                        "capability": "battery.power",
                                        "expected": 0.5,
                                        "tolerance": 0.1,
                                        "settle_timeout_seconds": 5.0,
                                        "poll_interval_seconds": 0.25,
                                    }
                                ],
                            }
                        ],
                    }
                },
            )
        )
        assert validated["validation"]["status"] == "requires_confirmation"
        approval = _structured(
            await server.call_tool(
                "request_approval",
                {
                    "plan_id": validated["plan"]["id"],
                    "validation_digest": validated["validation"]["digest"],
                    "operator_token": LIVE_OPERATOR_TOKEN,
                },
            )
        )
        scheduled = _structured(
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
        await asyncio.sleep(max(0.0, (execute_at - datetime.now(UTC)).total_seconds()) + 0.25)
        result = await runtime.scheduler.run_due()
        assert result == [{"plan_id": validated["plan"]["id"], "outcome": "executed"}]
        assert [write.value for write in transport.writes] == [0.5, 0.0]

        persisted = await runtime.plan_repository.get(validated["plan"]["id"])
        assert persisted is not None
        assert persisted.status.value == "completed"
        assert persisted.execution is not None
        assert persisted.execution.outcomes[0].status.value == "confirmed_success"
        assert persisted.execution.outcomes[0].after_state is not None
        assert persisted.execution.outcomes[0].after_state.value == pytest.approx(0.5, abs=0.1)

        async with httpx.AsyncClient(timeout=5) as client:
            final_state = await client.get(f"{battery_url}/state")
            final_state.raise_for_status()
            assert final_state.json()["power_kw"] == pytest.approx(0.0, abs=0.01)
    finally:
        if runtime is not None:
            await runtime.close()
        else:
            await adapter.disconnect()

    verification_database = SQLiteDatabase(database_path)
    await verification_database.initialize()
    try:
        persisted = await PlanRepository(verification_database).get(
            "live-mcp-knx-battery-charge"
        )
        assert persisted is not None
        assert persisted.status.value == "completed"
        outcomes = await ExecutionOutcomeRepository(verification_database).list_for_plan(
            "live-mcp-knx-battery-charge"
        )
        assert len(outcomes) == 1
        assert outcomes[0].status.value == "confirmed_success"
    finally:
        await verification_database.close()
