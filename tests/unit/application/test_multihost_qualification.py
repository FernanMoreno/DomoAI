from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from domoai.application.coordination import DeterministicLeaseCoordinator
from domoai.domain.coordination import FencingToken, LeaseScope
from domoai.domain.multihost_qualification import GatewayFencingProbeResult
from domoai.runtime.clock import FixedClock


class _QuorumProbe:
    def __init__(self, member_count: int = 3) -> None:
        self.member_count = member_count

    async def inspect(self):
        from domoai.application.multihost_qualification import EtcdQuorumObservation

        return EtcdQuorumObservation(
            cluster_id="cluster-1",
            member_ids=tuple(f"member-{index}" for index in range(self.member_count)),
            leader_id="member-0",
        )


class _PostgresProbe:
    async def inspect(self):
        from domoai.application.multihost_qualification import PostgresHaObservation

        return PostgresHaObservation(is_primary=True, synchronous_replicas=1)


class _FencingGateway:
    def __init__(self) -> None:
        self.highest_epoch = 0
        self.calls: list[int] = []

    async def probe(
        self, token: FencingToken, *, safe_command: str
    ) -> GatewayFencingProbeResult:
        self.calls.append(token.epoch)
        accepted = token.epoch > self.highest_epoch
        if accepted:
            self.highest_epoch = token.epoch
        return GatewayFencingProbeResult(
            probe_id=UUID(int=0),
            accepted=accepted,
            observed_epoch=self.highest_epoch or token.epoch,
            reason=None if accepted else "stale_epoch",
        )


@pytest.mark.asyncio
async def test_runner_emits_passed_evidence_after_quorum_takeover_and_fencing() -> None:
    from domoai.application.multihost_qualification import MultiHostQualificationRunner

    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    clock = FixedClock(now)
    gateway = _FencingGateway()
    runner = MultiHostQualificationRunner(
        coordinator=DeterministicLeaseCoordinator(clock=clock),
        quorum_probe=_QuorumProbe(),
        postgres_probe=_PostgresProbe(),
        gateway=gateway,
        clock=clock,
    )

    evidence = await runner.run(
        scope=LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        owner_id="host-a",
        gateway_identity="gateway-1",
        safe_command="safe-noop",
        ttl_seconds=30,
        evidence_ttl=timedelta(days=7),
    )

    assert evidence.status == "passed"
    assert [check.status for check in evidence.checks] == ["passed"] * len(evidence.checks)
    assert gateway.calls == [1, 2, 1, 2]


@pytest.mark.asyncio
async def test_runner_fails_quorum_without_touching_the_physical_gateway() -> None:
    from domoai.application.multihost_qualification import MultiHostQualificationRunner

    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    clock = FixedClock(now)
    gateway = _FencingGateway()
    runner = MultiHostQualificationRunner(
        coordinator=DeterministicLeaseCoordinator(clock=clock),
        quorum_probe=_QuorumProbe(member_count=2),
        postgres_probe=_PostgresProbe(),
        gateway=gateway,
        clock=clock,
    )

    evidence = await runner.run(
        scope=LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        owner_id="host-a",
        gateway_identity="gateway-1",
        safe_command="safe-noop",
        ttl_seconds=30,
        evidence_ttl=timedelta(days=7),
    )

    assert evidence.status == "failed"
    assert (
        next(check for check in evidence.checks if check.check_id == "etcd_quorum").status
        == "failed"
    )
    assert gateway.calls == []
