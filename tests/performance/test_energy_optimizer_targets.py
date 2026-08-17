from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Horizon, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for, flexible_load


@pytest.mark.asyncio
async def test_energy_optimizer_acceptance_target_for_ten_loads() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    store = StateStore()
    await DiscoveryService(adapter, registry, store, AuditLog()).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    horizon = Horizon(
        start=datetime(2026, 8, 15, tzinfo=UTC),
        end=datetime(2026, 8, 16, tzinfo=UTC),
        resolution_minutes=15,
        timezone="Europe/Madrid",
    )
    context = energy_context_for(horizon)
    scenario = OptimizationScenario(
        id="energy-performance-1",
        horizon=horizon,
        energy_context=context,
        loads=[
            flexible_load(
                device_id,
                load_id=f"load-{index}",
                power_kw=0.2 + (index % 3) * 0.1,
                earliest_slot=index,
                latest_slot=index + 2,
            )
            for index in range(10)
        ],
    )

    result = CpSatOptimizer(registry).optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.plan is not None
