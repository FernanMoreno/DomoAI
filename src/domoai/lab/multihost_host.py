"""Disposable host participant for the multi-host Docker qualification lab."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import psycopg
from pydantic import Field, model_validator

from domoai.application.etcd_coordination import EtcdHttpLeaseCoordinator
from domoai.domain.coordination import (
    FencingToken,
    LeaseScope,
    PhysicalIntent,
    PhysicalIntentStatus,
)
from domoai.domain.models import AuditEvent, AuthorityContext, PrincipalRole, StrictModel
from domoai.hil.multihost import JsonlFencingGatewayBridge
from domoai.persistence.audit_outbox import AuditOutboxDispatcher
from domoai.persistence.coordination import MetricHistoryRepository, PhysicalIntentRepository
from domoai.persistence.postgres import PostgresDatabase
from domoai.persistence.repositories import AuditEventRepository
from domoai.runtime.clock import SystemClock

HostAction = Literal[
    "status",
    "acquire",
    "release",
    "physical_intent",
    "recover",
    "outbox_append",
    "outbox_dispatch",
    "metric_sample",
]


class HostRequest(StrictModel):
    """Allowlisted request accepted by one disposable host participant."""

    action: HostAction
    household_id: str = Field(default="lab-household", min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    plan_id: str = Field(default="lab-plan", min_length=1, max_length=128)
    command_id: str = Field(default="lab-command", min_length=1, max_length=128)
    crash_after_claim: bool = False
    fail_delivery: bool = False
    metric_name: str = Field(default="lab_fencing_observation_total", max_length=128)
    metric_value: float = 1.0

    @model_validator(mode="after")
    def validate_action_fields(self) -> HostRequest:
        if self.action == "physical_intent" and not self.idempotency_key:
            raise ValueError("physical_intent requires idempotency_key")
        if self.metric_value != self.metric_value or abs(self.metric_value) == float("inf"):
            raise ValueError("metric_value must be finite")
        return self


def _response(status: str, **fields: object) -> dict[str, object]:
    return {"status": status, **fields}


class HostAgent:
    """Small line-protocol host that composes the real shared-state ports."""

    def __init__(
        self,
        *,
        instance_id: str,
        etcd_endpoints: tuple[str, ...],
        postgres_dsn: str,
        bridge_state_file: Path,
        outbox_sink_file: Path,
    ) -> None:
        if not instance_id or any(not endpoint.strip() for endpoint in etcd_endpoints):
            raise ValueError("lab host identity and coordinator endpoints are required")
        self.instance_id = instance_id
        self.scope = LeaseScope(
            tenant_id="lab-tenant", household_id="lab-household", deployment_id="docker-lab-v2"
        )
        self.coordinator = EtcdHttpLeaseCoordinator(etcd_endpoints, request_timeout_seconds=3)
        self.database = PostgresDatabase(postgres_dsn, clock=SystemClock())
        self.physical_intents = PhysicalIntentRepository(self.database, clock=SystemClock())
        self.metrics = MetricHistoryRepository(self.database, clock=SystemClock())
        self.audit = AuditEventRepository(self.database)
        self.gateway = JsonlFencingGatewayBridge(
            (
                sys.executable,
                "/opt/domoai-lab/gateway_fencing_lab.py",
                "--state-file",
                str(bridge_state_file),
            )
        )
        self.outbox_sink_file = outbox_sink_file
        self.token: FencingToken | None = None

    async def start(self) -> None:
        await self.database.initialize()

    async def close(self) -> None:
        await self.database.close()
        await self.coordinator.aclose()

    async def _reconnect_database(self) -> None:
        await self.database.close()
        await self.database.initialize()

    async def _database_call(self, operation: Any) -> Any:
        """Retry one idempotent lab operation after a Patroni connection reset."""

        for attempt in range(3):
            try:
                result = operation()
                return await result if inspect.isawaitable(result) else result
            except psycopg.Error:
                if attempt == 2:
                    raise
                await self._reconnect_database()
                await asyncio.sleep(1)
        raise AssertionError("unreachable database retry state")

    async def handle(self, request: HostRequest) -> dict[str, object]:
        if request.action == "status":
            return _response(
                "ok",
                instance_id=self.instance_id,
                owner_active=self.token is not None,
                epoch=self.token.epoch if self.token is not None else None,
            )
        if request.action == "acquire":
            return await self._acquire(request.household_id)
        if request.action == "release":
            return await self._release()
        if request.action == "physical_intent":
            return await self._physical_intent(request)
        if request.action == "recover":
            return _response(
                "ok", recovered=await self._database_call(self.physical_intents.recover_inflight)
            )
        if request.action == "outbox_append":
            return await self._outbox_append(request)
        if request.action == "outbox_dispatch":
            return await self._outbox_dispatch(request.fail_delivery)
        if request.action == "metric_sample":
            await self._database_call(
                lambda: self.metrics.append(
                    instance_id=self.instance_id,
                    process_start_time=SystemClock().now(),
                    metric_name=request.metric_name,
                    value=request.metric_value,
                    labels={"instance": self.instance_id, "household": request.household_id},
                    max_samples=8,
                )
            )
            return _response(
                "ok",
                metric_count=await self._database_call(
                    lambda: self.metrics.count(instance_id=self.instance_id)
                ),
            )
        raise AssertionError(f"unhandled host action: {request.action}")

    async def _acquire(self, household_id: str) -> dict[str, object]:
        scope = self.scope.model_copy(update={"household_id": household_id})
        try:
            self.token = await self.coordinator.acquire(
                scope, owner_id=self.instance_id, ttl_seconds=6
            )
        except Exception as error:
            return _response("rejected", reason=type(error).__name__)
        return _response("acquired", epoch=self.token.epoch, instance_id=self.instance_id)

    async def _release(self) -> dict[str, object]:
        if self.token is None:
            return _response("ok", released=False)
        token = self.token
        try:
            await self.coordinator.release(token)
        except Exception as error:
            return _response("rejected", reason=type(error).__name__)
        self.token = None
        return _response("ok", released=True)

    async def _physical_intent(self, request: HostRequest) -> dict[str, object]:
        token = self.token
        if token is None:
            return _response("rejected", reason="no_active_owner")
        try:
            await self.coordinator.validate(token)
        except Exception as error:
            return _response("rejected", reason=type(error).__name__)
        now = SystemClock().now()
        intent = PhysicalIntent(
            tenant_id=token.scope.tenant_id,
            household_id=token.scope.household_id,
            deployment_id=token.scope.deployment_id,
            idempotency_key=request.idempotency_key or "missing",
            plan_id=request.plan_id,
            command_id=request.command_id,
            fencing_epoch=token.epoch,
            created_at=now,
            updated_at=now,
        )
        claimed = await self._database_call(lambda: self.physical_intents.claim(intent))
        if claimed.status is not PhysicalIntentStatus.PREPARED:
            return _response("duplicate", intent_status=claimed.status.value, epoch=token.epoch)
        if request.crash_after_claim:
            os._exit(97)
        result = await self.gateway.probe(token, safe_command="lab-safe-noop")
        settled = await self._database_call(
            lambda: self.physical_intents.settle(
                household_id=token.scope.household_id,
                idempotency_key=intent.idempotency_key,
                status=(
                    PhysicalIntentStatus.CONFIRMED
                    if result.accepted
                    else PhysicalIntentStatus.REJECTED
                ),
            )
        )
        return _response(
            "accepted" if result.accepted else "rejected",
            intent_status=settled.status.value,
            epoch=token.epoch,
        )

    async def _outbox_append(self, request: HostRequest) -> dict[str, object]:
        event = AuditEvent(
            id=f"lab-outbox-{uuid4().hex}",
            event_type="plan_execution_started",
            actor=self.instance_id,
            subject_id=request.plan_id,
            payload={"scenario": "lab-v2"},
            authority=AuthorityContext(
                tenant_id="lab-tenant",
                household_id=request.household_id,
                household_ids=[request.household_id],
                principal_id=self.instance_id,
                roles=[PrincipalRole.SERVICE],
            ),
            created_at=SystemClock().now(),
        )
        await self._database_call(lambda: self.audit.append_event(event))
        return _response("ok", event_id=event.id)

    async def _outbox_dispatch(self, fail_delivery: bool) -> dict[str, object]:
        async def deliver(event: AuditEvent) -> None:
            if fail_delivery:
                raise RuntimeError("lab_sink_unavailable")
            self.outbox_sink_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.outbox_sink_file.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.seek(0)
                existing = {line.strip() for line in handle if line.strip()}
                if event.id not in existing:
                    handle.seek(0, os.SEEK_END)
                    handle.write(event.id + "\n")
                    handle.flush()
                fcntl.flock(handle, fcntl.LOCK_UN)

        result = await self._database_call(
            lambda: AuditOutboxDispatcher(self.audit, deliver).dispatch_once(limit=16)
        )
        return _response("ok", delivered=result.delivered, retried=result.retried)


async def _serve(args: argparse.Namespace) -> int:
    agent = HostAgent(
        instance_id=args.instance_id,
        etcd_endpoints=tuple(args.etcd_endpoint),
        postgres_dsn=args.postgres_dsn,
        bridge_state_file=Path(args.bridge_state_file),
        outbox_sink_file=Path(args.outbox_sink_file),
    )
    await agent.start()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            request = HostRequest.model_validate_json(raw)
            result = await agent.handle(request)
            writer.write((json.dumps(result, sort_keys=True) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as error:
            writer.write(
                (json.dumps(_response("rejected", reason=type(error).__name__)) + "\n").encode(
                    "utf-8"
                )
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "0.0.0.0", args.port)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await agent.close()
    return 0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DomoAI disposable multi-host lab participant")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--etcd-endpoint", action="append", required=True)
    parser.add_argument("--postgres-dsn", required=True)
    parser.add_argument("--bridge-state-file", required=True)
    parser.add_argument("--outbox-sink-file", required=True)
    args = parser.parse_args()
    if not args.serve or len(args.etcd_endpoint) != 3:
        parser.error("host agent requires --serve and exactly three etcd endpoints")
    return args


def main() -> int:
    try:
        return asyncio.run(_serve(_arguments()))
    except (OSError, RuntimeError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["HostAgent", "HostRequest"]
