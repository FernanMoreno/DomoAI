"""Provider-neutral physical control acquisition before dispatch writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol
from uuid import uuid4

from domoai.domain.models import (
    Command,
    ControlLeaseStatus,
    StrictModel,
    TakeoverResult,
)
from domoai.runtime.clock import Clock, SystemClock


class ControlTakeoverRequest(StrictModel):
    """Exact request sent to an adapter's provider-specific takeover hook."""

    owner: str
    device_id: str
    plan_id: str
    first_command_id: str
    first_command: str
    first_command_value: bool | int | float | str | None = None
    native_scheduler_status: str
    allow_native_takeover: bool
    lease_seconds: float


class ControlTakeoverAdapter(Protocol):
    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult: ...


class ControlPolicyPort(Protocol):
    owner: str
    native_scheduler_status: str
    allow_native_takeover: bool
    lease_seconds: float


class ControlTakeoverPort(Protocol):
    async def acquire_for_plan(
        self, *, plan_id: str, commands: Sequence[Command]
    ) -> TakeoverResult | None: ...


class BatteryControlCoordinator:
    """Gate battery plans on a provider-confirmed control lease.

    The coordinator never writes a battery command itself. It only admits a
    plan after the adapter has returned baseline, ownership and first-readback
    evidence. This keeps the executor as the sole physical write boundary.
    """

    def __init__(
        self,
        adapter: ControlTakeoverAdapter,
        policy: ControlPolicyPort,
        *,
        device_id: str = "battery.home",
        command_names: frozenset[str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.adapter = adapter
        self.policy = policy
        self.device_id = device_id
        self.command_names = command_names or frozenset(
            {"charge_battery", "discharge_battery", "stop_battery"}
        )
        self.clock = clock or SystemClock()
        self._results: dict[tuple[str, str, str], TakeoverResult] = {}

    async def acquire_for_plan(
        self, *, plan_id: str, commands: Sequence[Command]
    ) -> TakeoverResult | None:
        relevant = [
            command
            for command in commands
            if command.device_id == self.device_id
            and command.command in self.command_names
        ]
        if not relevant:
            return None
        first = relevant[0]
        key = (self.policy.owner, self.device_id, plan_id)
        existing = self._results.get(key)
        if existing is not None:
            return existing

        if self.policy.native_scheduler_status in {"active", "unknown"} and not (
            self.policy.allow_native_takeover
        ):
            result = self._rejected(
                plan_id=plan_id,
                first_command=first,
                failure_code=(
                    "native_owner_active"
                    if self.policy.native_scheduler_status == "active"
                    else "native_owner_unknown"
                ),
            )
            self._results[key] = result
            return result

        request = ControlTakeoverRequest(
            owner=self.policy.owner,
            device_id=self.device_id,
            plan_id=plan_id,
            first_command_id=first.id,
            first_command=first.command,
            first_command_value=first.value,
            native_scheduler_status=self.policy.native_scheduler_status,
            allow_native_takeover=self.policy.allow_native_takeover,
            lease_seconds=self.policy.lease_seconds,
        )
        try:
            result = await self.adapter.acquire_control(request)
        except Exception as error:
            result = self._rejected(
                plan_id=plan_id,
                first_command=first,
                failure_code="control_provider_unavailable",
            )
            result = result.model_copy(update={"evidence_digest": _digest(str(error))})
        if (
            result.owner != self.policy.owner
            or result.device_id != self.device_id
            or result.plan_id != plan_id
            or result.first_command_id != first.id
        ):
            result = self._rejected(
                plan_id=plan_id,
                first_command=first,
                failure_code="control_evidence_mismatch",
            )
        self._results[key] = result
        return result

    def _rejected(
        self, *, plan_id: str, first_command: Command, failure_code: str
    ) -> TakeoverResult:
        now = self.clock.now()
        payload = {
            "owner": self.policy.owner,
            "device_id": self.device_id,
            "plan_id": plan_id,
            "first_command_id": first_command.id,
            "failure_code": failure_code,
        }
        return TakeoverResult(
            lease_id=f"rejected-{uuid4()}",
            status=ControlLeaseStatus.REJECTED,
            owner=self.policy.owner,
            device_id=self.device_id,
            plan_id=plan_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=self.policy.lease_seconds),
            first_command_id=first_command.id,
            failure_code=failure_code,
            evidence_digest=_digest(payload),
        )


def _digest(payload: object) -> str:
    if isinstance(payload, str):
        canonical = payload
    else:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


__all__ = [
    "BatteryControlCoordinator",
    "ControlTakeoverAdapter",
    "ControlPolicyPort",
    "ControlTakeoverPort",
    "ControlTakeoverRequest",
]
