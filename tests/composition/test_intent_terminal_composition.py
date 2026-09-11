from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.coordination import DeterministicLeaseCoordinator
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.coordination import PhysicalIntentStatus
from domoai.domain.errors import DomainError
from domoai.domain.models import Command, Plan, PlanStatus


@pytest.mark.composition
@pytest.mark.asyncio
@pytest.mark.parametrize("readback_timeout", [False, True])
async def test_executor_ack_readback_and_ledger_remain_consistent_after_recovery(
    tmp_path: Path,
    readback_timeout: bool,
) -> None:
    class FencedAdapter(SimulatedHomeAdapter):
        supports_fencing = True
        sent = False

        async def execute(self, command, context=None):
            acknowledgement = await super().execute(command, context)
            self.sent = True
            return acknowledgement

        async def read_state(self, source_refs):
            if self.sent and readback_timeout:
                raise TimeoutError("readback unavailable after physical send")
            return await super().read_state(source_refs)

    adapter = FencedAdapter()
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "runtime.sqlite3", multi_host_enabled=True),
        adapter=adapter,
        lease_coordinator=DeterministicLeaseCoordinator(),
    )
    try:
        light = next(
            device.id for device in runtime.registry.devices if device.type.value == "light"
        )
        plan = runtime.plan_service.validate(
            Plan(
                id="terminal-plan",
                commands=[
                    Command(
                        id="terminal-command",
                        device_id=light,
                        command="turn_on",
                        idempotency_key="terminal-key",
                    )
                ],
            )
        )
        await runtime.facade.execute_plan(plan)
        stored_plan = await runtime.plan_repository.get(plan.id)
        ledger = await runtime.physical_intent_repository.get(
            household_id=plan.authority.household_id,
            idempotency_key="terminal-key",
        )
        assert ledger.status is (
            PhysicalIntentStatus.UNKNOWN if readback_timeout else PhysicalIntentStatus.CONFIRMED
        )
        assert stored_plan.status is (
            PlanStatus.UNKNOWN if readback_timeout else PlanStatus.COMPLETED
        )
        assert (
            await runtime.physical_intent_repository.recover_inflight(
                deployment_id=runtime.coordination_token.scope.deployment_id,
                fencing_epoch=runtime.coordination_token.epoch + 1,
            )
            == 0
        )
        assert (
            await runtime.physical_intent_repository.get(
                household_id=plan.authority.household_id,
                idempotency_key="terminal-key",
            )
            == ledger
        )
        with pytest.raises(DomainError):
            await runtime.facade.execute_plan(plan)
        assert len(adapter.calls) == 1
    finally:
        await runtime.close()
