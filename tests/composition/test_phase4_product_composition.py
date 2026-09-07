from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, Plan
from domoai.optimizer.ports import OptimizationStatus, build_result
from domoai.optimizer.product import build_product_summary
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def _context() -> tuple[SimulatedHomeAdapter, DeviceRegistry, StateStore, PlanService]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    return adapter, registry, state_store, PlanService(
        registry, state_store, PolicyEngine([]), audit
    )


async def test_guaranteed_readback_flows_from_registry_to_plan_validation() -> None:
    adapter = SimulatedHomeAdapter()
    snapshot = await adapter.discover()
    climate = next(
        entity for entity in snapshot.source_entities if entity["domain"] == "climate"
    )
    target_temperature = next(
        capability
        for capability in climate["capabilities"]
        if capability["name"] == "target_temperature"
    )
    target_temperature["guarantees"] = {"readback_required": True}
    registry = DeviceRegistry()
    registry.apply_snapshot(snapshot, adapter.adapter_id)
    state_store = StateStore()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), AuditLog())
    device = next(device for device in registry.devices if device.type.value == "climate")
    command = Command(
        id="guaranteed-command",
        device_id=device.id,
        command="set_temperature",
        value=21,
        unit="°C",
        idempotency_key="guaranteed-intent",
    )

    result = plan_service.validate(Plan(id="guaranteed-plan", commands=[command]))

    assert result.validation is not None
    assert any(error.field == "postconditions" for error in result.validation.errors)


async def test_product_projection_keeps_optimizer_and_adapter_boundaries_separate() -> None:
    adapter, _registry, _state_store, _plan_service = await _context()
    result = build_result(
        scenario_id="product-composition-1",
        status=OptimizationStatus.INFEASIBLE,
        diagnostics=[{"code": "infeasible", "message": "bounded fixture"}],
    )

    summary = build_product_summary(result)

    assert summary.proposal_id is None
    assert adapter.calls == []
