"""Deterministic coordination provider used by tests and simulations."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

from domoai.domain.coordination import (
    FencingToken,
    FencingViolation,
    LeaseCoordinator,
    LeaseScope,
    LeaseUnavailable,
)
from domoai.domain.models import AuthorityContext
from domoai.runtime.clock import Clock, SystemClock


def _scope_key(scope: LeaseScope) -> tuple[str, str, str]:
    return scope.tenant_id, scope.household_id, scope.deployment_id


class DeterministicLeaseCoordinator(LeaseCoordinator):
    """In-process coordinator for race and partition tests.

    This class is intentionally not wired from production settings. It models
    the semantics an external coordinator must provide: one live owner per
    scope and a strictly increasing epoch after expiry/takeover.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self._leases: dict[tuple[str, str, str], FencingToken] = {}
        self._epochs: dict[tuple[str, str, str], int] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self, scope: LeaseScope, *, owner_id: str, ttl_seconds: float
    ) -> FencingToken:
        if not owner_id.strip():
            raise ValueError("lease owner_id must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive")
        async with self._lock:
            key = _scope_key(scope)
            current = self._leases.get(key)
            now = self.clock.now()
            if current is not None and current.expires_at > now:
                raise LeaseUnavailable(f"lease scope is owned by {current.owner_id}")
            epoch = self._epochs.get(key, 0) + 1
            token = FencingToken(
                scope=scope,
                owner_id=owner_id,
                epoch=epoch,
                lease_id=uuid4().hex,
                issued_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._epochs[key] = epoch
            self._leases[key] = token
            return token

    async def renew(self, token: FencingToken) -> FencingToken:
        async with self._lock:
            current = self._leases.get(_scope_key(token.scope))
            self._assert_current(token, current)
            now = self.clock.now()
            if token.expires_at <= now:
                raise FencingViolation("lease is expired")
            renewed = token.model_copy(
                update={"expires_at": now + (token.expires_at - token.issued_at)}
            )
            self._leases[_scope_key(token.scope)] = renewed
            return renewed

    async def release(self, token: FencingToken) -> None:
        async with self._lock:
            current = self._leases.get(_scope_key(token.scope))
            self._assert_current(token, current)
            self._leases.pop(_scope_key(token.scope), None)

    async def validate(self, token: FencingToken) -> None:
        async with self._lock:
            current = self._leases.get(_scope_key(token.scope))
            self._assert_current(token, current)
            if token.expires_at <= self.clock.now():
                raise FencingViolation("lease is expired")

    @staticmethod
    def _assert_current(token: FencingToken, current: FencingToken | None) -> None:
        if current is None:
            raise FencingViolation("lease is unknown")
        if current.epoch != token.epoch:
            raise FencingViolation("stale fencing token")
        if current.owner_id != token.owner_id:
            raise FencingViolation("lease owner is not current")
        if current.lease_id != token.lease_id:
            raise FencingViolation("stale fencing token")


class FencingGuard:
    """Validate one lease immediately before a physical write."""

    def __init__(self, coordinator: LeaseCoordinator, token: FencingToken) -> None:
        self.coordinator = coordinator
        self.token = token
        self.lost = False

    async def assert_writable(self, scope: LeaseScope) -> FencingToken:
        if self.lost:
            raise FencingViolation("fencing lease has been lost")
        if scope != self.token.scope:
            raise FencingViolation("fencing scope does not match physical target scope")
        await self.coordinator.validate(self.token)
        return self.token

    async def assert_writable_for_authority(self, authority: AuthorityContext) -> FencingToken:
        return await self.assert_writable(
            LeaseScope(
                tenant_id=authority.tenant_id,
                household_id=authority.household_id,
                deployment_id=self.token.scope.deployment_id,
            )
        )

    def replace_token(self, token: FencingToken) -> None:
        if token.scope != self.token.scope or token.epoch < self.token.epoch:
            raise FencingViolation("fencing renewal moved backwards or changed scope")
        self.token = token

    def mark_lost(self) -> None:
        self.lost = True
