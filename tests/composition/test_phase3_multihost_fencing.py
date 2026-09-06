from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.coordination import DeterministicLeaseCoordinator
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import Command, Plan
from domoai.runtime.clock import FixedClock
from domoai.runtime.execution_context import ExecutionContext


class _ContextAdapter(SimulatedHomeAdapter):
    supports_fencing = True

    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[ExecutionContext | None] = []

    async def execute(self, command, execution_context=None):
        self.contexts.append(execution_context)
        return await super().execute(command, execution_context)


def _plan(runtime, plan_id: str, *, idempotency_key: str | None = None) -> Plan:
    device_id = next(
        device.id for device in runtime.registry.devices if device.type.value == "light"
    )
    return runtime.plan_service.validate(
        Plan(
            id=plan_id,
            commands=[
                Command(
                    id=f"command-{plan_id}",
                    device_id=device_id,
                    command="turn_on",
                    idempotency_key=idempotency_key or f"intent-{plan_id}",
                )
            ],
        )
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_current_epoch_reaches_adapter_and_stale_host_is_blocked(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    clock = FixedClock(now)
    coordinator = DeterministicLeaseCoordinator(clock=clock)
    adapter = _ContextAdapter()
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "runtime.sqlite3",
            multi_host_enabled=True,
            coordination_lease_seconds=10,
        ),
        adapter=adapter,
        clock=clock,
        lease_coordinator=coordinator,
    )
    try:
        current = _plan(runtime, "current-epoch")
        summary = await runtime.facade.execute_plan(current)
        assert summary.outcomes[0].status.value in {"confirmed_success", "unknown"}
        assert adapter.contexts[0] is not None
        assert adapter.contexts[0].fencing_epoch == runtime.coordination_token.epoch

        replay = _plan(
            runtime,
            "replay-epoch",
            idempotency_key="intent-current-epoch",
        )
        await runtime.facade.execute_plan(replay)
        assert len(adapter.contexts) == 1

        clock.set(now + timedelta(seconds=11))
        await coordinator.acquire(
            runtime.fencing_guard.token.scope,
            owner_id="takeover-host",
            ttl_seconds=10,
        )
        stale = _plan(runtime, "stale-epoch")
        with pytest.raises(DomainError) as error:
            await runtime.facade.execute_plan(stale)

        assert error.value.code is ErrorCode.FENCING_VIOLATION
        assert len(adapter.contexts) == 1
    finally:
        await runtime.close()
