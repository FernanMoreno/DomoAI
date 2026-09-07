import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.coordination import DeterministicLeaseCoordinator
from domoai.domain.coordination import FencingViolation, LeaseScope, LeaseUnavailable
from domoai.runtime.clock import FixedClock


def _scope() -> LeaseScope:
    return LeaseScope(tenant_id="tenant-a", household_id="home-a", deployment_id="gw-a")


@pytest.mark.asyncio
async def test_two_hosts_racing_for_one_scope_have_one_winner() -> None:
    coordinator = DeterministicLeaseCoordinator()

    results = await asyncio.gather(
        coordinator.acquire(_scope(), owner_id="host-a", ttl_seconds=30),
        coordinator.acquire(_scope(), owner_id="host-b", ttl_seconds=30),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, LeaseUnavailable) for result in results) == 1


@pytest.mark.asyncio
async def test_takeover_increments_epoch_and_rejects_old_token() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    clock = FixedClock(now)
    coordinator = DeterministicLeaseCoordinator(clock=clock)
    old = await coordinator.acquire(_scope(), owner_id="host-a", ttl_seconds=10)

    clock.set(now + timedelta(seconds=11))
    current = await coordinator.acquire(_scope(), owner_id="host-b", ttl_seconds=10)

    assert current.epoch == old.epoch + 1
    with pytest.raises(FencingViolation, match="stale"):
        await coordinator.validate(old)


@pytest.mark.asyncio
async def test_renewal_requires_current_owner_and_epoch() -> None:
    coordinator = DeterministicLeaseCoordinator()
    token = await coordinator.acquire(_scope(), owner_id="host-a", ttl_seconds=10)

    with pytest.raises(FencingViolation, match="owner"):
        await coordinator.renew(token.model_copy(update={"owner_id": "host-b"}))

    renewed = await coordinator.renew(token)
    assert renewed.epoch == token.epoch
    assert renewed.expires_at > token.expires_at
