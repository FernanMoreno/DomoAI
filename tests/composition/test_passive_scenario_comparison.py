import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_service import OptimizationService
from domoai.application.optimization_worker import OptimizationWorker
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.mcp.ortools_server import OrtoolsMcpContext, create_ortools_server
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.contract.test_ortools_mcp_contract import structured
from tests.integration.test_counterfactual_analysis import passive_energy_scenario


@pytest.mark.parametrize("currency", ["EUR", "USD"])
async def test_passive_mcp_worker_solver_comparison_has_no_runtime_effects(
    monkeypatch: pytest.MonkeyPatch,
    currency: str,
) -> None:
    adapter = SimulatedHomeAdapter()
    registry, state, audit = DeviceRegistry(), StateStore(), AuditLog()
    await DiscoveryService(adapter, registry, state, audit).refresh()
    plans = PlanService(registry, state, PolicyEngine([]), audit)
    service = OptimizationService(registry, plans, CpSatOptimizer(registry))
    worker = OptimizationWorker(service)
    context = OrtoolsMcpContext(registry, plans, service, optimization_worker=worker)
    server = create_ortools_server(context)
    baseline = passive_energy_scenario("composed-passive-base")
    alternative = passive_energy_scenario("composed-passive-alternative", price=0.4)
    for point in alternative.energy_context.tariffs:
        point.currency = currency
    snapshots = [snapshot.model_dump() for snapshot in await state.all()]
    revision, events = context.runtime_revision, audit.events
    # A comparison must never reach plan creation, even without physical devices.
    def unexpected_plan_creation(*args: object, **kwargs: object) -> None:
        raise AssertionError("Read-only comparison attempted to create a runtime plan")

    monkeypatch.setattr(plans, "create_plan", unexpected_plan_creation)
    try:
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert tools["compare_scenarios"].annotations.readOnlyHint is True
        for _ in range(2):
            result = structured(await server.call_tool("compare_scenarios", {
                "baseline": baseline.model_dump(mode="json"),
                "variations": {"higher_tariff": alternative.model_dump(mode="json")},
            }))
            assert result["baseline"]["status"] == "no_action_required"
            diff = result["variations"]["higher_tariff"]["diff"]
            accounting = result["variations"]["higher_tariff"]["summary"]["constraint_summary"][
                "accounting"
            ]["comparison"]
            if currency == "EUR":
                assert diff["energy_cost"] == pytest.approx(0.1)
                assert accounting["diff_units"]["energy_cost"] == "EUR"
            else:
                assert "energy_cost" not in diff
                assert accounting["omitted_objectives"]["energy_cost"] == "currency_mismatch"
            assert result["runtime_revision"] == revision
        assert adapter.calls == []
        assert context.runtime_revision == revision
        assert [snapshot.model_dump() for snapshot in await state.all()] == snapshots
        assert audit.events == events
    finally:
        worker.close()
