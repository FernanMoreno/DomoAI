from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.models import (
    Command,
    ControlLeaseStatus,
    PhysicalBaseline,
    SourceRef,
    TakeoverResult,
)
from domoai.optimizer.energy import BatteryControlPolicy
from domoai.runtime.control_takeover import (
    BatteryControlCoordinator,
    ControlTakeoverRequest,
)


class FakeControlAdapter:
    def __init__(self, result: TakeoverResult) -> None:
        self.result = result
        self.requests: list[ControlTakeoverRequest] = []

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        self.requests.append(request)
        return self.result


def _result(*, confirmed: bool = True) -> TakeoverResult:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    return TakeoverResult(
        lease_id="lease-1",
        status=(ControlLeaseStatus.ACQUIRED if confirmed else ControlLeaseStatus.REJECTED),
        owner="domoai",
        device_id="battery.home",
        plan_id="plan-1",
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
        baseline=PhysicalBaseline(
            device_id="battery.home",
            capability="battery.power",
            power_kw=2.0,
            observed_at=now,
            received_at=now,
            state_revision="power:4",
            source_ref=SourceRef(adapter_id="fixture", external_id="battery.power"),
            native_scheduler_status="active",
        ),
        first_command_id="command-1",
        first_command_confirmed=confirmed,
        confirmed_at=now if confirmed else None,
        failure_code=None if confirmed else "takeover_readback_failed",
        evidence_digest="sha256:evidence",
    )


@pytest.mark.asyncio
async def test_battery_coordinator_requests_control_for_first_command() -> None:
    adapter = FakeControlAdapter(_result())
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(
            owner="domoai",
            native_scheduler_status="active",
            allow_native_takeover=True,
        ),
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="stop_battery",
        idempotency_key="command-key",
    )

    result = await coordinator.acquire_for_plan(
        plan_id="plan-1", commands=[command]
    )

    assert result is not None and result.first_command_confirmed
    assert adapter.requests[0].first_command_id == "command-1"
    assert adapter.requests[0].allow_native_takeover is True


@pytest.mark.asyncio
async def test_unknown_native_owner_fails_closed_without_adapter_call() -> None:
    adapter = FakeControlAdapter(_result())
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="unknown"),
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="stop_battery",
        idempotency_key="command-key",
    )

    result = await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    assert result is not None
    assert result.status is ControlLeaseStatus.REJECTED
    assert result.failure_code == "native_owner_unknown"
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_unconfirmed_first_readback_is_not_acquired() -> None:
    adapter = FakeControlAdapter(_result(confirmed=False))
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="stop_battery",
        idempotency_key="command-key",
    )

    result = await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    assert result is not None
    assert result.status is ControlLeaseStatus.REJECTED
    assert result.first_command_confirmed is False
