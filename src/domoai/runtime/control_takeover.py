"""Provider-neutral physical control acquisition before dispatch writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import uuid4

from domoai.domain.models import (
    Command,
    ControlLeaseStatus,
    PhysicalBaseline,
    SourceRef,
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
    native_scheduler_status: Literal["disabled", "inactive", "active", "unknown"]
    allow_native_takeover: bool
    lease_seconds: float


class ControlTakeoverAdapter(Protocol):
    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult: ...


class ControlPolicyPort(Protocol):
    owner: str
    native_scheduler_status: Literal["disabled", "inactive", "active", "unknown"]
    allow_native_takeover: bool
    lease_seconds: float


class ControlTakeoverPort(Protocol):
    async def acquire_for_plan(
        self, *, plan_id: str, commands: Sequence[Command]
    ) -> TakeoverResult | None: ...

    async def assert_still_owned(self, *, plan_id: str) -> bool: ...

    async def emergency_stop(self, *, plan_id: str, execution_attempt_id: str) -> bool: ...

    async def release_for_plan(self, *, plan_id: str, execution_attempt_id: str) -> bool: ...


class ControlSupervisorPort(ControlTakeoverPort, Protocol):
    async def supervise_once(self) -> list[str]: ...

    async def shutdown(self) -> None: ...


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
        power_feedback_source_ref: SourceRef | None = None,
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
        self.power_feedback_source_ref = power_feedback_source_ref
        self.power_feedback_tolerance_kw = power_feedback_tolerance_kw
        self.clock = clock or SystemClock()
        self._results: dict[tuple[str, str, str], TakeoverResult] = {}
        # A configured physical feedback path is also a startup safety
        # dependency.  Runtime construction must reconcile it before a new
        # lease can be acquired; a coordinator without that path is retained
        # for provider-neutral/non-latched takeover tests and has no startup
        # reconciliation step to perform.
        self._startup_reconciled = (
            state_store is None and power_feedback_capability is None
        )

    @property
    def startup_reconciled(self) -> bool:
        """Whether this coordinator has passed its startup safety gate."""

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
        if not self._startup_reconciled:
            # Do not cache this rejection: a successful explicit startup
            # reconciliation must be able to admit the same plan afterwards.
            return self._rejected(
                plan_id=plan_id,
                first_command=first,
                failure_code="startup_reconciliation_required",
            )
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

        # A physical battery has one control owner, not one owner per plan.
        # Do not let direct execution or concurrent scheduler paths create a
        # second lease while the first one is still live.  Expired records are
        # retired here so a dead lease cannot permanently block recovery.
        for held_key, held in list(self._results.items()):
            if held_key[:2] != key[:2] or held.status is not ControlLeaseStatus.ACQUIRED:
                continue
            if self.clock.now() >= held.expires_at:
                self._results[held_key] = held.model_copy(
                    update={"status": ControlLeaseStatus.EXPIRED, "failure_code": "lease_expired"}
                )
                continue
            return self._rejected(
                plan_id=plan_id,
                first_command=first,
                failure_code="control_lease_already_held",
            )

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
            return False
        renewal_margin = max(1.0, self.policy.lease_seconds * 0.2)
        if result.expires_at - now <= timedelta(seconds=renewal_margin):
            renew = getattr(self.adapter, "renew_control", None)
            if not callable(renew):
                # Adapters that do not expose renewal still have a valid
                # bounded lease.  They fail closed at the actual expiry; the
                # absence of an optional renewal API must not make a freshly
                # acquired short test lease unusable.
                return self.clock.now() < result.expires_at
            try:
                renewed = await renew(result)
            except Exception:
                renewed = None
            if not isinstance(renewed, TakeoverResult):
                return False
            if (
                renewed.status is not ControlLeaseStatus.ACQUIRED
                or renewed.owner != self.policy.owner
                or renewed.device_id != self.device_id
                or renewed.plan_id != plan_id
                or renewed.first_command_id != result.first_command_id
            ):
                return False
            self._results[(self.policy.owner, self.device_id, plan_id)] = renewed
            result = renewed
        return self.clock.now() < result.expires_at

    async def emergency_stop(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        """Stop a latched actuator after ownership loss or failed execution.

        This is the one supervisor-owned write allowed outside the normal
        executor path: it is a fail-safe cleanup operation, carries its own
        idempotency key, and reports success only when the adapter accepts the
        stop request.
        """

        key = (self.policy.owner, self.device_id, plan_id)
        result = self._results.get(key)
        execute = getattr(self.adapter, "execute", None)
        if result is None:
            return False
        if not callable(execute):
            self._revoke(key, result, "control_provider_unavailable")
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
            self._revoke(key, result, "emergency_stop_failed")
            return False
        if not getattr(acknowledgement, "accepted", False):
            self._revoke(key, result, "emergency_stop_rejected")
            return False
        # A transport ACK is not physical evidence.  Treat a coordinator
        # missing its configured feedback path as a failed stop and revoke the
        # lease so a later caller cannot mistake it for live ownership.
        if self.state_store is None or self.power_feedback_capability is None:
            self._revoke(key, result, "emergency_stop_readback_not_configured")
            return False
        baseline = result.baseline
        read_state = getattr(self.adapter, "read_state", None)
        if baseline is None or not callable(read_state):
            self._revoke(key, result, "emergency_stop_readback_unavailable")
            return False
        try:
            matching = await self._read_feedback(baseline.source_ref)
        except Exception:
            self._revoke(key, result, "emergency_stop_readback_failed")
            return False
        if not self._feedback_is_safe(matching):
            self._revoke(key, result, "emergency_stop_readback_unconfirmed")
            return False
        self._results[key] = result.model_copy(update={"status": ControlLeaseStatus.RELEASED})
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

        if self.state_store is None and self.power_feedback_capability is None:
            self._startup_reconciled = True
            return True
        if self.state_store is None or self.power_feedback_capability is None:
            self._startup_reconciled = False
            return False
        snapshot = self.state_store.peek(self.device_id, self.power_feedback_capability)
        source_ref = snapshot.source_ref if snapshot is not None else self.power_feedback_source_ref
        if source_ref is None:
            self._startup_reconciled = False
            return False
        if self._feedback_is_safe(snapshot):
            try:
                live_snapshot = await self._read_feedback(source_ref)
            except Exception:
                live_snapshot = None
            if self._feedback_is_safe(live_snapshot):
                self._startup_reconciled = True
                return True
        elif snapshot is None:
            # A first boot or a persistence gap must still reconcile the
            # physical route directly.  Cached state is useful evidence, but
            # it is never a prerequisite for the one live read that decides
            # whether a latched actuator needs a stop.
            try:
                live_snapshot = await self._read_feedback(source_ref)
            except Exception:
                live_snapshot = None
            if self._feedback_is_safe(live_snapshot):
                self._startup_reconciled = True
                return True
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
                self._startup_reconciled = False
                return False
            read_state = getattr(self.adapter, "read_state", None)
            if not callable(read_state):
                self._startup_reconciled = False
                return False
            matching = await self._read_feedback(source_ref)
            confirmed = self._feedback_is_safe(matching)
            self._startup_reconciled = confirmed
            return confirmed
        except Exception:
            self._startup_reconciled = False
            return False

    async def _read_feedback(self, source_ref: SourceRef) -> StateSnapshot | None:
        """Read the exact feedback route and normalize only its device ID.

        Adapters may report their provider-local device identity while the
        runtime's binding uses a canonical ID.  The source reference is the
        authoritative route identity at this boundary.
        """

        if self.power_feedback_capability is None:
            return None
        read_state = getattr(self.adapter, "read_state", None)
        if not callable(read_state):
            return None
        snapshots = await read_state([source_ref])
        for item in snapshots:
            if (
                isinstance(item, StateSnapshot)
                and
                _same_source_route(item.source_ref, source_ref)
                and item.capability == self.power_feedback_capability
            ):
                return item.model_copy(update={"device_id": self.device_id})
        return None

    def _feedback_is_safe(self, snapshot: StateSnapshot | None) -> bool:
        if snapshot is None or snapshot.status is not StateStatus.CURRENT:
            return False
        if not isinstance(snapshot.value, (int, float)) or isinstance(snapshot.value, bool):
            return False
        age_seconds = (self.clock.now() - snapshot.received_at).total_seconds()
        if self.state_store is not None and (
            age_seconds < 0 or age_seconds > self.state_store.stale_after.total_seconds()
        ):
            return False
        return abs(float(snapshot.value)) <= self.power_feedback_tolerance_kw

    async def release_for_plan(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        key = (self.policy.owner, self.device_id, plan_id)
        result = self._results.get(key)
        if result is None:
            return False
        if result.status is not ControlLeaseStatus.ACQUIRED:
            return result.status is ControlLeaseStatus.RELEASED
        if self.state_store is None or self.power_feedback_capability is None:
            self._results[key] = result.model_copy(update={"status": ControlLeaseStatus.RELEASED})
            return True
        return await self.emergency_stop(
            plan_id=plan_id,
            execution_attempt_id=execution_attempt_id,
        )

    async def shutdown(self) -> None:
        """Stop every still-owned latched actuator before runtime shutdown."""

        for key, result in list(self._results.items()):
            if result.status is not ControlLeaseStatus.ACQUIRED:
                continue
            confirmed = await self.emergency_stop(
                plan_id=result.plan_id,
                execution_attempt_id=f"control-supervisor:shutdown:{result.lease_id}",
            )
            if not confirmed:
                self._results[key] = result.model_copy(
                    update={
                        "status": ControlLeaseStatus.EXPIRED,
                        "failure_code": "shutdown_stop_unconfirmed",
                    }
                )

    def _revoke(
        self,
        key: tuple[str, str, str],
        result: TakeoverResult,
        failure_code: str,
    ) -> None:
        """Revoke authority after any unverified emergency-stop path."""

        self._results[key] = result.model_copy(
            update={"status": ControlLeaseStatus.EXPIRED, "failure_code": failure_code}
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


class _EVReadbackControlAdapter:
    """Adapt a concrete runtime adapter to the EV lease contract.

    EV providers do not currently expose a native scheduler takeover hook.
    The explicit EV binding therefore authorizes one concrete command route,
    while this adapter still requires a live numeric readback before a lease
    is admitted.  It deliberately never treats an adapter connection or
    transport ACK as ownership evidence.
    """

    def __init__(
        self,
        adapter: object,
        *,
        source_ref: SourceRef | None,
        feedback_capability: str,
        stale_after: timedelta,
        clock: Clock,
    ) -> None:
        self._adapter = adapter
        self._source_ref = source_ref
        self._feedback_capability = feedback_capability
        self._stale_after = stale_after
        self._clock = clock

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        now = self._clock.now()
        if self._source_ref is None:
            return self._rejected(request, now, "baseline_unavailable")
        read_state = getattr(self._adapter, "read_state", None)
        if not callable(read_state):
            return self._rejected(request, now, "control_provider_unavailable")
        try:
            snapshots = await read_state([self._source_ref])
        except Exception:
            return self._rejected(request, now, "baseline_unavailable")
        # The provider stamps ``received_at`` while completing the read.  The
        # JIT freshness decision must use a clock value taken after that I/O;
        # otherwise a few microseconds of normal scheduling can make a fresh
        # read appear to come from the future.
        now = self._clock.now()
        snapshot = next(
            (
                item
                for item in snapshots
                if isinstance(item, StateSnapshot)
                and _same_source_route(item.source_ref, self._source_ref)
                and item.capability == self._feedback_capability
            ),
            None,
        )
        if not self._is_fresh_numeric(snapshot, now):
            return self._rejected(request, now, "baseline_unavailable")
        assert snapshot is not None
        baseline = PhysicalBaseline(
            device_id=request.device_id,
            capability=self._feedback_capability,
            power_kw=float(cast(int | float, snapshot.value)),
            observed_at=snapshot.observed_at,
            received_at=snapshot.received_at,
            source_ref=snapshot.source_ref,
            state_revision=f"ev:{snapshot.received_at.isoformat()}",
            native_scheduler_status=request.native_scheduler_status,
        )
        return TakeoverResult(
            lease_id=f"ev-control-{request.device_id}-{request.plan_id}",
            status=ControlLeaseStatus.ACQUIRED,
            owner=request.owner,
            device_id=request.device_id,
            plan_id=request.plan_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=request.lease_seconds),
            baseline=baseline,
            first_command_id=request.first_command_id,
            first_command_confirmed=False,
            evidence_digest=_digest(
                {"request": request.model_dump(mode="json"), "baseline": baseline}
            ),
        )

    async def execute(self, command: Command, execution_context: ExecutionContext) -> object:
        execute = getattr(self._adapter, "execute", None)
        if not callable(execute):
            raise ConnectionError("EV control provider does not expose execute")
        return cast(object, await execute(command, execution_context))

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        read_state = getattr(self._adapter, "read_state", None)
        if not callable(read_state):
            raise ConnectionError("EV control provider does not expose read_state")
        return cast(list[StateSnapshot], await read_state(source_refs))

    def _is_fresh_numeric(self, snapshot: StateSnapshot | None, now: datetime) -> bool:
        if snapshot is None or snapshot.status is not StateStatus.CURRENT:
            return False
        if not isinstance(snapshot.value, (int, float)) or isinstance(snapshot.value, bool):
            return False
        age = now - snapshot.received_at
        return timedelta(0) <= age <= self._stale_after

    @staticmethod
    def _rejected(
        request: ControlTakeoverRequest, now: datetime, failure_code: str
    ) -> TakeoverResult:
        payload = {
            "request": request.model_dump(mode="json"),
            "failure_code": failure_code,
        }
        return TakeoverResult(
            lease_id=f"rejected-{uuid4()}",
            status=ControlLeaseStatus.REJECTED,
            owner=request.owner,
            device_id=request.device_id,
            plan_id=request.plan_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=request.lease_seconds),
            first_command_id=request.first_command_id,
            failure_code=failure_code,
            evidence_digest=_digest(payload),
        )


class EVControlCoordinator(BatteryControlCoordinator):
    """Lease and supervise an explicitly bound EV charging route."""

    def __init__(
        self,
        adapter: object,
        policy: ControlPolicyPort,
        *,
        device_id: str,
        command_names: frozenset[str],
        stop_command: str,
        stop_unit: str,
        state_store: StateStorePort,
        power_feedback_capability: str,
        power_feedback_source_ref: SourceRef | None,
        power_feedback_tolerance_kw: float = 0.05,
        clock: Clock | None = None,
    ) -> None:
        runtime_clock = clock or SystemClock()
        super().__init__(
            _EVReadbackControlAdapter(
                adapter,
                source_ref=power_feedback_source_ref,
                feedback_capability=power_feedback_capability,
                stale_after=state_store.stale_after,
                clock=runtime_clock,
            ),
            policy,
            device_id=device_id,
            command_names=command_names,
            stop_command=stop_command,
            stop_unit=stop_unit,
            state_store=state_store,
            power_feedback_capability=power_feedback_capability,
            power_feedback_source_ref=power_feedback_source_ref,
            power_feedback_tolerance_kw=power_feedback_tolerance_kw,
            clock=runtime_clock,
        )


class ControlTakeoverGroup:
    """Compose independent physical leases behind one executor boundary."""

    def __init__(self, coordinators: Sequence[ControlSupervisorPort]) -> None:
        self.coordinators = tuple(coordinators)
        self._active: dict[str, tuple[ControlSupervisorPort, ...]] = {}

    async def acquire_for_plan(
        self, *, plan_id: str, commands: Sequence[Command]
    ) -> TakeoverResult | None:
        acquired: list[ControlSupervisorPort] = []
        first_result: TakeoverResult | None = None
        for coordinator in self.coordinators:
            result = await coordinator.acquire_for_plan(plan_id=plan_id, commands=commands)
            if result is None:
                continue
            if result.status is not ControlLeaseStatus.ACQUIRED:
                for held in acquired:
                    await held.release_for_plan(
                        plan_id=plan_id,
                        execution_attempt_id=f"{plan_id}:takeover-rollback",
                    )
                return result
            acquired.append(coordinator)
            first_result = first_result or result
        if not acquired:
            return None
        self._active[plan_id] = tuple(acquired)
        return first_result

    async def assert_still_owned(self, *, plan_id: str) -> bool:
        active = self._active.get(plan_id)
        if active is None:
            return True
        for coordinator in active:
            if not await coordinator.assert_still_owned(plan_id=plan_id):
                return False
        return True

    async def emergency_stop(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        active = self._active.get(plan_id)
        if active is None:
            return True
        results = [
            await coordinator.emergency_stop(
                plan_id=plan_id,
                execution_attempt_id=execution_attempt_id,
            )
            for coordinator in active
        ]
        return all(results)

    async def release_for_plan(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        active = self._active.pop(plan_id, None)
        if active is None:
            return True
        results = [
            await coordinator.release_for_plan(
                plan_id=plan_id,
                execution_attempt_id=execution_attempt_id,
            )
            for coordinator in active
        ]
        return all(results)

    async def supervise_once(self) -> list[str]:
        stopped: set[str] = set()
        for coordinator in self.coordinators:
            stopped.update(await coordinator.supervise_once())
        return sorted(stopped)

    async def shutdown(self) -> None:
        for coordinator in self.coordinators:
            await coordinator.shutdown()


def _digest(payload: object) -> str:
    if isinstance(payload, str):
        canonical = payload
    else:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _same_source_route(left: SourceRef, right: SourceRef) -> bool:
    """Compare stable source identity, ignoring optional discovery metadata."""

    return left.adapter_id == right.adapter_id and left.external_id == right.external_id


__all__ = [
    "BatteryControlCoordinator",
    "ControlSupervisorPort",
    "ControlTakeoverGroup",
    "ControlTakeoverAdapter",
    "ControlPolicyPort",
    "ControlTakeoverPort",
    "ControlTakeoverRequest",
    "EVControlCoordinator",
]
