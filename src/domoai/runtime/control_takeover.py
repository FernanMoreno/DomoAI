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
    StateSnapshot,
    StateStatus,
    StrictModel,
    TakeoverResult,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.ports import StateStorePort


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

    async def assert_still_owned(self, *, plan_id: str) -> bool: ...

    async def emergency_stop(self, *, plan_id: str, execution_attempt_id: str) -> bool: ...


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
        stop_command: str = "stop_battery",
        stop_unit: str = "kW",
        state_store: StateStorePort | None = None,
        power_feedback_capability: str | None = None,
        power_feedback_tolerance_kw: float = 0.05,
        clock: Clock | None = None,
    ) -> None:
        self.adapter = adapter
        self.policy = policy
        self.device_id = device_id
        self.command_names = command_names or frozenset(
            {"charge_battery", "discharge_battery", "stop_battery"}
        )
        self.stop_command = stop_command
        self.stop_unit = stop_unit
        self.state_store = state_store
        self.power_feedback_capability = power_feedback_capability
        self.power_feedback_tolerance_kw = power_feedback_tolerance_kw
        self.clock = clock or SystemClock()
        self._results: dict[tuple[str, str, str], TakeoverResult] = {}
        self._authority_blocked = False
        self._authority_block_reason: str | None = None
        self._startup_reconciled: bool | None = None

    @property
    def authority_blocked(self) -> bool:
        """Whether new physical battery orders are currently forbidden."""

        return self._authority_blocked

    @property
    def authority_block_reason(self) -> str | None:
        return self._authority_block_reason

    @property
    def startup_reconciled(self) -> bool | None:
        return self._startup_reconciled

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
            if (
                existing.status is ControlLeaseStatus.ACQUIRED
                and self.clock.now() >= existing.expires_at
            ):
                expired = existing.model_copy(
                    update={
                        "status": ControlLeaseStatus.EXPIRED,
                        "failure_code": "control_lease_expired",
                    }
                )
                self._results[key] = expired
                return expired
            return existing
        if self._authority_blocked:
            result = self._rejected(
                plan_id=plan_id,
                first_command=first,
                failure_code="control_authority_blocked",
            )
            self._results[key] = result
            return result

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

    async def assert_still_owned(self, *, plan_id: str) -> bool:
        """JIT lease check used immediately before each physical write."""

        result = self._results.get((self.policy.owner, self.device_id, plan_id))
        if result is None or result.status is not ControlLeaseStatus.ACQUIRED:
            return False
        now = self.clock.now()
        if now >= result.expires_at:
            self._results[(self.policy.owner, self.device_id, plan_id)] = result.model_copy(
                update={"status": ControlLeaseStatus.EXPIRED, "failure_code": "lease_expired"}
            )
            await self.emergency_stop(
                plan_id=plan_id,
                execution_attempt_id=f"control-supervisor:{result.lease_id}",
            )
            return False
        renewal_margin = max(1.0, self.policy.lease_seconds * 0.2)
        if result.expires_at - now <= timedelta(seconds=renewal_margin):
            renew = getattr(self.adapter, "renew_control", None)
            if not callable(renew):
                await self.emergency_stop(
                    plan_id=plan_id,
                    execution_attempt_id=f"control-supervisor:{result.lease_id}",
                )
                return False
            try:
                renewed = await renew(result)
            except Exception:
                renewed = None
            if not isinstance(renewed, TakeoverResult):
                await self.emergency_stop(
                    plan_id=plan_id,
                    execution_attempt_id=f"control-supervisor:{result.lease_id}",
                )
                return False
            if (
                renewed.status is not ControlLeaseStatus.ACQUIRED
                or renewed.owner != self.policy.owner
                or renewed.device_id != self.device_id
                or renewed.plan_id != plan_id
                or renewed.first_command_id != result.first_command_id
            ):
                await self.emergency_stop(
                    plan_id=plan_id,
                    execution_attempt_id=f"control-supervisor:{result.lease_id}",
                )
                return False
            self._results[(self.policy.owner, self.device_id, plan_id)] = renewed
            result = renewed
        return self.clock.now() < result.expires_at

    async def emergency_stop(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        """Stop a latched actuator after ownership loss or failed execution.

        This is the one supervisor-owned write allowed outside the normal
        executor path: it is a fail-safe cleanup operation, carries its own
        idempotency key, and reports success only when a current zero-power
        readback confirms the stop.
        """

        key = (self.policy.owner, self.device_id, plan_id)
        result = self._results.get(key)
        execute = getattr(self.adapter, "execute", None)
        if (
            result is not None
            and result.status is ControlLeaseStatus.RELEASED
            and result.failure_code == "emergency_stop_confirmed"
        ):
            return True
        if (
            result is None
            or result.status not in {ControlLeaseStatus.ACQUIRED, ControlLeaseStatus.EXPIRED}
            or not callable(execute)
        ):
            return False
        command = Command(
            id=f"{plan_id}:emergency-stop",
            device_id=self.device_id,
            command=self.stop_command,
            value=0,
            unit=self.stop_unit,
            idempotency_key=f"{plan_id}:emergency-stop:{result.lease_id}",
            intent="control_supervisor_emergency_stop",
        )
        try:
            acknowledgement = await execute(
                command,
                ExecutionContext(
                    plan_id=plan_id,
                    execution_attempt_id=execution_attempt_id,
                    adapter_request_id=f"{plan_id}:emergency-stop-request",
                ),
            )
        except Exception:
            self._results[key] = result.model_copy(
                update={
                    "status": ControlLeaseStatus.EXPIRED,
                    "failure_code": "emergency_stop_failed",
                }
            )
            self._block_authority("emergency_stop_failed")
            return False
        if not getattr(acknowledgement, "accepted", False):
            self._results[key] = result.model_copy(
                update={
                    "status": ControlLeaseStatus.EXPIRED,
                    "failure_code": "emergency_stop_rejected",
                }
            )
            self._block_authority("emergency_stop_failed")
            return False
        read_state = getattr(self.adapter, "read_state", None)
        source_ref = result.baseline.source_ref if result.baseline is not None else None
        if not callable(read_state) or source_ref is None or self.power_feedback_capability is None:
            self._results[key] = result.model_copy(
                update={
                    "status": ControlLeaseStatus.EXPIRED,
                    "failure_code": "emergency_stop_readback_failed",
                }
            )
            self._block_authority("emergency_stop_failed")
            return False
        try:
            snapshots = await read_state([source_ref])
            matching = next(
                (
                    item
                    for item in snapshots
                    if isinstance(item, StateSnapshot)
                    and item.capability == self.power_feedback_capability
                    and item.source_ref == source_ref
                ),
                None,
            )
            if matching is None or not self._is_safe_power_readback(matching):
                raise ValueError("emergency stop readback is not zero")
            matching = matching.model_copy(update={"device_id": self.device_id})
            if self.state_store is not None:
                await self.state_store.save(matching)
        except Exception:
            self._results[key] = result.model_copy(
                update={
                    "status": ControlLeaseStatus.EXPIRED,
                    "failure_code": "emergency_stop_readback_failed",
                }
            )
            self._block_authority("emergency_stop_failed")
            return False
        self._results[key] = result.model_copy(
            update={
                "status": ControlLeaseStatus.RELEASED,
                "failure_code": "emergency_stop_confirmed",
            }
        )
        self._block_authority("lease_supervision_required")
        return True

    async def supervise_once(self) -> list[str]:
        """Renew active leases or stop before an unrenewable lease expires.

        A latched inverter command must never outlive the runtime's ownership
        evidence.  Providers may implement an optional ``renew_control``
        hook; when they do not, the supervisor deliberately stops the
        actuator before the lease deadline instead of silently allowing the
        command to remain active.
        """

        stopped: list[str] = []
        now = self.clock.now()
        margin = timedelta(seconds=max(1.0, self.policy.lease_seconds * 0.2))
        for key, result in list(self._results.items()):
            if result.status is not ControlLeaseStatus.ACQUIRED:
                continue
            remaining = result.expires_at - now
            if remaining > margin:
                continue
            renew = getattr(self.adapter, "renew_control", None)
            renewed: TakeoverResult | None = None
            if callable(renew) and remaining > timedelta(0):
                try:
                    candidate = await renew(result)
                except Exception:
                    candidate = None
                if (
                    isinstance(candidate, TakeoverResult)
                    and candidate.status is ControlLeaseStatus.ACQUIRED
                    and candidate.owner == self.policy.owner
                    and candidate.device_id == self.device_id
                    and candidate.plan_id == result.plan_id
                    and candidate.first_command_id == result.first_command_id
                ):
                    renewed = candidate
            if renewed is not None:
                self._results[key] = renewed
                continue
            stop_confirmed = await self.emergency_stop(
                plan_id=result.plan_id,
                execution_attempt_id=f"control-supervisor:{result.lease_id}",
            )
            self._results[key] = result.model_copy(
                update={
                    "status": (
                        ControlLeaseStatus.RELEASED
                        if stop_confirmed
                        else ControlLeaseStatus.EXPIRED
                    ),
                    "failure_code": (
                        "lease_supervisor_stop_confirmed"
                        if stop_confirmed
                        else "lease_supervisor_stop_failed"
                    ),
                }
            )
            stopped.append(result.plan_id)
        return stopped

    async def reconcile_startup(self) -> bool:
        """Reconcile a possibly latched actuator after process recovery.

        A persisted ``EXECUTING -> UNKNOWN`` transition deliberately prevents
        command replay, but a latched inverter may still be delivering power.
        If a last-known feedback snapshot exists, issue one idempotent stop
        and require a zero readback before reporting reconciliation success.
        """

        if self.state_store is None or self.power_feedback_capability is None:
            self._block_authority("startup_reconciliation_unavailable")
            self._startup_reconciled = False
            return False
        snapshot = self.state_store.peek(self.device_id, self.power_feedback_capability)
        if snapshot is None:
            self._block_authority("startup_reconciliation_unavailable")
            self._startup_reconciled = False
            return False
        command = Command(
            id="startup-reconciliation-stop",
            device_id=self.device_id,
            command=self.stop_command,
            value=0,
            unit=self.stop_unit,
            idempotency_key="startup-reconciliation-stop",
            intent="control_supervisor_startup_reconciliation",
        )
        execute = getattr(self.adapter, "execute", None)
        if not callable(execute):
            self._block_authority("startup_reconciliation_failed")
            self._startup_reconciled = False
            return False
        try:
            acknowledgement = await execute(
                command,
                ExecutionContext(
                    plan_id="startup-reconciliation",
                    execution_attempt_id="startup-reconciliation",
                    adapter_request_id="startup-reconciliation-stop",
                ),
            )
            if not getattr(acknowledgement, "accepted", False):
                self._block_authority("startup_reconciliation_failed")
                self._startup_reconciled = False
                return False
            read_state = getattr(self.adapter, "read_state", None)
            if not callable(read_state):
                self._block_authority("startup_reconciliation_failed")
                self._startup_reconciled = False
                return False
            snapshots = await read_state([snapshot.source_ref])
            matching = next(
                (
                    item
                    for item in snapshots
                    if isinstance(item, StateSnapshot)
                    and item.capability == self.power_feedback_capability
                    and item.source_ref == snapshot.source_ref
                ),
                None,
            )
            if not isinstance(matching, StateSnapshot) or not self._is_safe_power_readback(
                matching
            ):
                self._block_authority("startup_reconciliation_failed")
                self._startup_reconciled = False
                return False
            matching = matching.model_copy(update={"device_id": self.device_id})
            await self.state_store.save(matching)
            self._startup_reconciled = True
            self._authority_blocked = False
            self._authority_block_reason = None
            return True
        except Exception:
            self._block_authority("startup_reconciliation_failed")
            self._startup_reconciled = False
            return False

    async def release_for_plan(self, *, plan_id: str) -> None:
        key = (self.policy.owner, self.device_id, plan_id)
        result = self._results.get(key)
        if result is None:
            return
        release = getattr(self.adapter, "release_control", None)
        if callable(release):
            try:
                await release(result)
            except Exception:
                self._block_authority("control_release_failed")
                return
        self._results[key] = result.model_copy(update={"status": ControlLeaseStatus.RELEASED})

    def _block_authority(self, reason: str) -> None:
        self._authority_blocked = True
        self._authority_block_reason = reason

    def _is_safe_power_readback(self, snapshot: StateSnapshot | None) -> bool:
        return bool(
            snapshot is not None
            and snapshot.status is StateStatus.CURRENT
            and isinstance(snapshot.value, (int, float))
            and not isinstance(snapshot.value, bool)
            and abs(float(snapshot.value)) <= self.power_feedback_tolerance_kw
        )

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
