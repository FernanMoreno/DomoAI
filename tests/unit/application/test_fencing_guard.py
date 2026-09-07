from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.coordination import DeterministicLeaseCoordinator, FencingGuard
from domoai.domain.coordination import FencingViolation, LeaseScope
from domoai.runtime.clock import FixedClock


def _scope() -> LeaseScope:
    return LeaseScope(tenant_id="tenant-a", household_id="home-a", deployment_id="gw-a")


@pytest.mark.asyncio
async def test_guard_allows_current_token_and_returns_non_secret_context() -> None:
    coordinator = DeterministicLeaseCoordinator()
    token = await coordinator.acquire(_scope(), owner_id="host-a", ttl_seconds=30)
    guard = FencingGuard(coordinator, token)

    assert await guard.assert_writable(_scope()) == token


@pytest.mark.asyncio
async def test_guard_rejects_token_after_takeover_before_adapter() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    clock = FixedClock(now)
    coordinator = DeterministicLeaseCoordinator(clock=clock)
    old = await coordinator.acquire(_scope(), owner_id="host-a", ttl_seconds=10)
    guard = FencingGuard(coordinator, old)

    clock.set(now + timedelta(seconds=11))
    await coordinator.acquire(_scope(), owner_id="host-b", ttl_seconds=10)

    with pytest.raises(FencingViolation, match="stale"):
        await guard.assert_writable(_scope())


@pytest.mark.asyncio
async def test_guard_rejects_different_household_scope() -> None:
    coordinator = DeterministicLeaseCoordinator()
    token = await coordinator.acquire(_scope(), owner_id="host-a", ttl_seconds=30)
    guard = FencingGuard(coordinator, token)

    with pytest.raises(FencingViolation, match="scope"):
        await guard.assert_writable(
            LeaseScope(tenant_id="tenant-a", household_id="home-b", deployment_id="gw-a")
        )
