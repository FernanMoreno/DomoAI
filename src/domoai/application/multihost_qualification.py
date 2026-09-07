"""Attended external coordination and physical-fencing qualification."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol

import httpx
import psycopg

from domoai.domain.coordination import (
    FencingToken,
    FencingViolation,
    LeaseCoordinator,
    LeaseScope,
)
from domoai.domain.multihost_qualification import (
    REQUIRED_MULTIHOST_CHECKS,
    GatewayFencingProbeResult,
    MultiHostQualificationCheck,
    MultiHostQualificationCheckStatus,
    MultiHostQualificationEvidence,
)
from domoai.runtime.clock import Clock, SystemClock


@dataclass(frozen=True)
class EtcdQuorumObservation:
    cluster_id: str
    member_ids: tuple[str, ...]
    leader_id: str


@dataclass(frozen=True)
class PostgresHaObservation:
    is_primary: bool
    synchronous_replicas: int


class EtcdQuorumProbePort(Protocol):
    async def inspect(self) -> EtcdQuorumObservation: ...


class PostgresHaProbePort(Protocol):
    async def inspect(self) -> PostgresHaObservation: ...


class FencingGatewayProbePort(Protocol):
    async def probe(
        self, token: FencingToken, *, safe_command: str
    ) -> GatewayFencingProbeResult: ...


class EtcdHttpQuorumProbe:
    """Read every configured etcd member's maintenance status."""

    def __init__(
        self,
        endpoints: Sequence[str],
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.endpoints = tuple(endpoint.rstrip("/") for endpoint in endpoints if endpoint.strip())
        if len(self.endpoints) < 3:
            raise ValueError("etcd qualification requires at least three endpoints")
        if timeout_seconds <= 0:
            raise ValueError("etcd qualification timeout must be positive")
        self.timeout = httpx.Timeout(timeout_seconds)
        self._client = client
        self._owns_client = client is None

    async def inspect(self) -> EtcdQuorumObservation:
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        if self._client is None:
            self._client = client
        statuses: list[tuple[str, str, str]] = []
        try:
            for endpoint in self.endpoints:
                response = await client.post(
                    f"{endpoint}/v3/maintenance/status", json={}, timeout=self.timeout
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("etcd status response is not an object")
                status = payload.get("result", payload)
                if not isinstance(status, dict):
                    raise RuntimeError("etcd status result is invalid")
                header = status.get("header")
                if not isinstance(header, dict):
                    raise RuntimeError("etcd status header is missing")
                cluster_id = str(header.get("cluster_id", ""))
                member_id = str(header.get("member_id", ""))
                leader_id = str(status.get("leader", ""))
                if not cluster_id or not member_id or not leader_id:
                    raise RuntimeError("etcd status identity is incomplete")
                statuses.append((cluster_id, member_id, leader_id))
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError("etcd member status is unavailable") from error
        clusters = {status[0] for status in statuses}
        if len(clusters) != 1:
            raise RuntimeError("etcd endpoints belong to different clusters")
        leaders = {status[2] for status in statuses}
        if len(leaders) != 1:
            raise RuntimeError("etcd endpoints disagree on leader")
        return EtcdQuorumObservation(
            cluster_id=statuses[0][0],
            member_ids=tuple(status[1] for status in statuses),
            leader_id=statuses[0][2],
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class PsycopgPostgresHaProbe:
    """Read primary and synchronous streaming-replica state without mutation."""

    _QUERY = """
        SELECT
            NOT pg_is_in_recovery() AS is_primary,
            COUNT(*) FILTER (
                WHERE state = 'streaming' AND sync_state = 'sync'
            ) AS synchronous_replicas
        FROM pg_stat_replication
    """

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL qualification DSN must be non-empty")
        self.dsn = dsn
        self._connection_factory = connection_factory or psycopg.connect

    async def inspect(self) -> PostgresHaObservation:
        return await asyncio.to_thread(self._inspect_sync)

    def _inspect_sync(self) -> PostgresHaObservation:
        connection = self._connection_factory(self.dsn)
        try:
            row = connection.execute(self._QUERY).fetchone()
            if row is None or len(row) != 2:
                raise RuntimeError("PostgreSQL HA query returned no row")
            return PostgresHaObservation(
                is_primary=bool(row[0]), synchronous_replicas=int(row[1])
            )
        finally:
            connection.close()


class MultiHostQualificationRunner:
    """Run a safe active-passive qualification without operating topology."""

    def __init__(
        self,
        *,
        coordinator: LeaseCoordinator,
        quorum_probe: EtcdQuorumProbePort,
        postgres_probe: PostgresHaProbePort,
        gateway: FencingGatewayProbePort,
        clock: Clock | None = None,
        qualification_environment: Literal["production", "lab"] = "production",
    ) -> None:
        if qualification_environment not in {"production", "lab"}:
            raise ValueError("qualification environment must be production or lab")
        self.coordinator = coordinator
        self.quorum_probe = quorum_probe
        self.postgres_probe = postgres_probe
        self.gateway = gateway
        self.clock = clock or SystemClock()
        self.qualification_environment = qualification_environment

    async def run(
        self,
        *,
        scope: LeaseScope,
        owner_id: str,
        gateway_identity: str,
        safe_command: str,
        ttl_seconds: float,
        evidence_ttl: timedelta,
    ) -> MultiHostQualificationEvidence:
        if ttl_seconds <= 1:
            raise ValueError("qualification lease TTL must be greater than one second")
        if evidence_ttl <= timedelta(0):
            raise ValueError("qualification evidence TTL must be positive")
        checks = {
            check_id: MultiHostQualificationCheck(
                check_id=check_id, status=MultiHostQualificationCheckStatus.BLOCKED
            )
            for check_id in REQUIRED_MULTIHOST_CHECKS
        }
        try:
            postgres = await self.postgres_probe.inspect()
            checks["postgres_primary"] = self._check(
                "postgres_primary", postgres.is_primary
            )
            checks["postgres_sync_replica"] = self._check(
                "postgres_sync_replica", postgres.synchronous_replicas >= 1
            )
        except Exception as error:
            checks["postgres_primary"] = self._failure("postgres_primary", error)
            checks["postgres_sync_replica"] = self._failure("postgres_sync_replica", error)
        try:
            quorum = await self.quorum_probe.inspect()
            quorum_ok = (
                len(quorum.member_ids) in {3, 5}
                and len(set(quorum.member_ids)) == len(quorum.member_ids)
                and quorum.leader_id in quorum.member_ids
                and bool(quorum.cluster_id.strip())
            )
            checks["etcd_quorum"] = self._check("etcd_quorum", quorum_ok)
        except Exception as error:
            checks["etcd_quorum"] = self._failure("etcd_quorum", error)

        if any(
            checks[check_id].status is MultiHostQualificationCheckStatus.FAILED
            for check_id in ("etcd_quorum", "postgres_primary", "postgres_sync_replica")
        ):
            return self._evidence(scope, gateway_identity, evidence_ttl, checks)

        first: FencingToken | None = None
        first_released = False
        takeover: FencingToken | None = None
        try:
            first = await self.coordinator.acquire(
                scope, owner_id=owner_id, ttl_seconds=ttl_seconds
            )
            renewed = await self.coordinator.renew(first)
            checks["etcd_lease_renewal"] = self._check(
                "etcd_lease_renewal", renewed.epoch == first.epoch
            )
            first_result = await self.gateway.probe(renewed, safe_command=safe_command)
            first_gateway_ok = (
                first_result.accepted and first_result.observed_epoch == renewed.epoch
            )
            await self.coordinator.release(renewed)
            first_released = True
            takeover = await self.coordinator.acquire(
                scope, owner_id=f"{owner_id}-takeover", ttl_seconds=ttl_seconds
            )
            try:
                await self.coordinator.validate(first)
            except FencingViolation:
                stale_coordinator_rejected = True
            else:
                stale_coordinator_rejected = False
            checks["etcd_takeover"] = self._check(
                "etcd_takeover",
                stale_coordinator_rejected and takeover.epoch > renewed.epoch,
            )
            takeover_result = await self.gateway.probe(takeover, safe_command=safe_command)
            stale_result = await self.gateway.probe(renewed, safe_command=safe_command)
            replay_result = await self.gateway.probe(takeover, safe_command=safe_command)
            checks["gateway_current_epoch"] = self._check(
                "gateway_current_epoch",
                first_gateway_ok
                and takeover_result.accepted
                and takeover_result.observed_epoch == takeover.epoch,
            )
            checks["gateway_stale_epoch"] = self._check(
                "gateway_stale_epoch",
                not stale_result.accepted and stale_result.observed_epoch >= takeover.epoch,
            )
            checks["gateway_replay_epoch"] = self._check(
                "gateway_replay_epoch",
                not replay_result.accepted and replay_result.observed_epoch == takeover.epoch,
            )
        except Exception as error:
            for check_id in (
                "etcd_lease_renewal",
                "etcd_takeover",
                "gateway_current_epoch",
                "gateway_stale_epoch",
                "gateway_replay_epoch",
            ):
                if checks[check_id].status is MultiHostQualificationCheckStatus.BLOCKED:
                    checks[check_id] = self._failure(check_id, error)
        finally:
            if takeover is not None:
                try:
                    await self.coordinator.release(takeover)
                except Exception:
                    pass
            elif first is not None and not first_released:
                try:
                    await self.coordinator.release(first)
                except Exception:
                    pass
        return self._evidence(scope, gateway_identity, evidence_ttl, checks)

    @staticmethod
    def _check(check_id: str, passed: bool) -> MultiHostQualificationCheck:
        return MultiHostQualificationCheck(
            check_id=check_id,
            status=(
                MultiHostQualificationCheckStatus.PASSED
                if passed
                else MultiHostQualificationCheckStatus.FAILED
            ),
        )

    @staticmethod
    def _failure(check_id: str, error: Exception) -> MultiHostQualificationCheck:
        return MultiHostQualificationCheck(
            check_id=check_id,
            status=MultiHostQualificationCheckStatus.FAILED,
            details={"error_type": type(error).__name__},
        )

    def _evidence(
        self,
        scope: LeaseScope,
        gateway_identity: str,
        evidence_ttl: timedelta,
        checks: dict[str, MultiHostQualificationCheck],
    ) -> MultiHostQualificationEvidence:
        completed_at = self.clock.now()
        return MultiHostQualificationEvidence(
            qualification_environment=self.qualification_environment,
            scope=scope,
            gateway_identity=gateway_identity,
            completed_at=completed_at,
            expires_at=completed_at + evidence_ttl,
            checks=[checks[check_id] for check_id in sorted(REQUIRED_MULTIHOST_CHECKS)],
        )


__all__ = [
    "EtcdQuorumObservation",
    "EtcdHttpQuorumProbe",
    "EtcdQuorumProbePort",
    "FencingGatewayProbePort",
    "MultiHostQualificationRunner",
    "PostgresHaObservation",
    "PostgresHaProbePort",
    "PsycopgPostgresHaProbe",
]
