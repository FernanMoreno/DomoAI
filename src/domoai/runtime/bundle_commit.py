"""Runtime-owned durable commit boundary for validated plan bundles."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    BundleMemberCommitStatus,
    ErrorDetail,
    ExecutionStatus,
    ExecutionSummary,
    Plan,
    PlanStatus,
    StrictModel,
)
from domoai.persistence.repositories import (
    BundleCommitRepository,
    PlanRepository,
    ScheduledPlanRepository,
)
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog


class BundleCommitRequestMember(StrictModel):
    plan_id: str = Field(min_length=1)
    validation_digest: str = Field(min_length=1)
    execute_at: datetime | None = None
    approval_id: str | None = None

    @model_validator(mode="after")
    def validate_timestamp(self) -> BundleCommitRequestMember:
        if self.execute_at is not None and self.execute_at.tzinfo is None:
            raise ValueError("execute_at must be timezone-aware")
        return self


class BundleCommitRequest(StrictModel):
    bundle_digest: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    members: list[BundleCommitRequestMember] = Field(min_length=1, max_length=50)


def bundle_approval_digest(
    scenario_id: str, members: list[BundleCommitRequestMember]
) -> str:
    """Build the canonical digest shared by the host and runtime boundary."""

    payload = {
        "schema": "bundle-approval-v1",
        "scenario_id": scenario_id,
        "members": [
            {
                "plan_id": member.plan_id,
                "validation_digest": member.validation_digest,
                "execute_at": member.execute_at.isoformat()
                if member.execute_at is not None
                else None,
            }
            for member in members
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class BundleCommitService:
    """Preflight and commit a bundle without bypassing PlanExecutor safety."""

    def __init__(
        self,
        *,
        facade: Any,
        plans: dict[str, Plan],
        approval_store: ApprovalStore,
        bundle_repository: BundleCommitRepository,
        scheduled_repository: ScheduledPlanRepository,
        audit: AuditLog,
        plan_repository: PlanRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.facade = facade
        self.plans = plans
        self.approval_store = approval_store
        self.bundle_repository = bundle_repository
        self.scheduled_repository = scheduled_repository
        self.audit = audit
        self.plan_repository = plan_repository
        self.clock = clock or SystemClock()

    async def commit(self, request: BundleCommitRequest) -> BundleCommit:
        expected_digest = bundle_approval_digest(request.scenario_id, request.members)
        if request.bundle_digest != expected_digest:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "Bundle digest does not match the ordered member evidence",
            )

        existing = await self.bundle_repository.get_by_digest(request.bundle_digest)
        if existing is not None:
            return existing

        plans = [
            await self._preflight_member(member, bundle_digest=request.bundle_digest)
            for member in request.members
        ]
        bundle = BundleCommit(
            id=f"bundle-commit-{uuid4().hex}",
            bundle_digest=request.bundle_digest,
            scenario_id=request.scenario_id,
            members=[
                BundleMemberCommit(
                    plan_id=member.plan_id,
                    validation_digest=member.validation_digest,
                    execute_at=member.execute_at,
                )
                for member in request.members
            ],
        )
        try:
            bundle = await self.bundle_repository.save(bundle)
        except Exception:
            existing = await self.bundle_repository.get_by_digest(request.bundle_digest)
            if existing is not None:
                return existing
            raise
        self.audit.append(
            event_type="bundle_commit_started",
            actor="runtime",
            subject_id=bundle.id,
            payload={
                "bundle_digest": bundle.bundle_digest,
                "scenario_id": bundle.scenario_id,
                "member_count": len(bundle.members),
            },
        )

        try:
            plans = await self._consume_approvals(
                request.members, plans, bundle_digest=request.bundle_digest
            )
        except Exception as error:
            return await self._finish_failure(bundle, error, committed=False)
        try:
            self._assert_all_executable(plans)
        except Exception as error:
            return await self._finish_failure(bundle, error, committed=False)

        now = self.clock.now()
        due_indexes = [
            index
            for index, member in enumerate(request.members)
            if member.execute_at is None or member.execute_at <= now
        ]
        future_indexes = [
            index for index, member in enumerate(request.members) if index not in due_indexes
        ]

        if not due_indexes:
            try:
                return await self.bundle_repository.schedule_members_transaction(
                    bundle,
                    plans,
                    future_indexes,
                    final_status=BundleCommitStatus.SCHEDULED,
                )
            except Exception as error:
                return await self._finish_failure(bundle, error, committed=False)

        for index in due_indexes:
            plan = plans[index]
            try:
                summary = await self.facade.execute_plan(plan)
            except Exception as error:
                bundle = await self._mark_member(
                    bundle,
                    index,
                    status=BundleMemberCommitStatus.UNKNOWN,
                    error_code="execution_unknown",
                    details={"error": str(error)[:200]},
                )
                return await self._finish_failure(bundle, error, committed=any(
                    item.status is BundleMemberCommitStatus.EXECUTED for item in bundle.members
                ), unknown=True)

            member_status, execution_status = self._classify_execution(summary)
            bundle = await self._mark_member(
                bundle,
                index,
                status=member_status,
                execution_status=execution_status,
            )
            if member_status is not BundleMemberCommitStatus.EXECUTED:
                return await self._finish_failure(
                    bundle,
                    RuntimeError("bundle member execution was not confirmed"),
                    committed=any(
                        item.status is BundleMemberCommitStatus.EXECUTED
                        for item in bundle.members
                    ),
                    unknown=member_status is BundleMemberCommitStatus.UNKNOWN,
                )

        if future_indexes:
            try:
                return await self.bundle_repository.schedule_members_transaction(
                    bundle,
                    [plans[index] for index in future_indexes],
                    future_indexes,
                    final_status=BundleCommitStatus.COMPLETED,
                )
            except Exception as error:
                return await self._finish_failure(bundle, error, committed=True)

        return await self.bundle_repository.save(
            bundle.model_copy(update={"status": BundleCommitStatus.COMPLETED})
        )

    async def _preflight_member(
        self, member: BundleCommitRequestMember, *, bundle_digest: str
    ) -> Plan:
        plan = self.plans.get(member.plan_id)
        if plan is None and self.plan_repository is not None:
            plan = await self.plan_repository.get(member.plan_id)
            if plan is not None:
                self.plans[plan.id] = plan
        if plan is None:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"Unknown plan: {member.plan_id}")
        if plan.validation is None or plan.validation.digest != member.validation_digest:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "Member validation digest mismatch")
        if plan.execute_at != member.execute_at:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "Member execute_at mismatch")
        if plan.status not in {
            PlanStatus.READY,
            PlanStatus.APPROVED,
            PlanStatus.REQUIRES_CONFIRMATION,
        }:
            raise DomainError(ErrorCode.INVALID_TRANSITION, "Member plan is not executable")
        if plan.status is PlanStatus.REQUIRES_CONFIRMATION:
            if member.approval_id is None:
                raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Member approval is required")
            self.approval_store.validate(
                member.approval_id, plan, bundle_digest=bundle_digest
            )
        self._assert_current_route(plan)
        return plan

    def _assert_all_executable(self, plans: list[Plan]) -> None:
        plan_service = getattr(self.facade, "plan_service", None)
        if plan_service is None:
            return
        for plan in plans:
            plan_service.assert_executable(plan)

    def _assert_current_route(self, plan: Plan) -> None:
        plan_service = getattr(self.facade, "plan_service", None)
        if plan_service is None:
            return
        route_errors = {
            "device_not_found": ErrorCode.DEVICE_NOT_FOUND,
            "unsupported_command": ErrorCode.UNSUPPORTED_COMMAND,
            "ambiguous_route": ErrorCode.ROUTE_AMBIGUOUS,
            "route_not_found": ErrorCode.ROUTE_NOT_FOUND,
            "source_unavailable": ErrorCode.SOURCE_UNAVAILABLE,
        }
        for command in plan.commands:
            route = plan_service.registry.resolve_command_route(
                command.device_id, command.command
            )
            if route.reason is not None:
                code = route_errors.get(route.reason, ErrorCode.ROUTE_NOT_FOUND)
                raise DomainError(
                    code,
                    f"No executable route for command {command.command!r}",
                    device_id=command.device_id,
                )

    async def _consume_approvals(
        self,
        members: list[BundleCommitRequestMember],
        plans: list[Plan],
        *,
        bundle_digest: str,
    ) -> list[Plan]:
        approved = list(plans)
        grants: list[tuple[int, Any]] = []
        for index, member in enumerate(members):
            if member.approval_id is not None:
                grants.append(
                    (
                        index,
                        self.approval_store.consume(
                            member.approval_id,
                            plans[index],
                            bundle_digest=bundle_digest,
                        ),
                    )
                )
        for index, grant in grants:
            approved[index] = self.facade.approve_plan(plans[index], grant=grant)
            self.plans[approved[index].id] = approved[index]
            if self.plan_repository is not None:
                await self.plan_repository.save(approved[index])
        return approved

    async def _mark_member(
        self,
        bundle: BundleCommit,
        index: int,
        *,
        status: BundleMemberCommitStatus,
        execution_status: ExecutionStatus | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> BundleCommit:
        members = list(bundle.members)
        members[index] = members[index].model_copy(
            update={
                "status": status,
                "execution_status": execution_status,
                "error_code": error_code,
                "details": details or {},
            }
        )
        return await self.bundle_repository.save(bundle.model_copy(update={"members": members}))

    async def _finish_failure(
        self,
        bundle: BundleCommit,
        error: Exception,
        *,
        committed: bool,
        unknown: bool = False,
    ) -> BundleCommit:
        status = (
            BundleCommitStatus.UNKNOWN
            if unknown and not committed
            else (
                BundleCommitStatus.PARTIALLY_COMMITTED
                if committed
                else BundleCommitStatus.FAILED
            )
        )
        detail = ErrorDetail(
            code="bundle_commit_unknown" if unknown else "bundle_commit_failed",
            message=str(error)[:200] or "Bundle commit failed",
            retryable=unknown,
        )
        result = await self.bundle_repository.save(
            bundle.model_copy(update={"status": status, "failure": detail})
        )
        self.audit.append(
            event_type="bundle_commit_completed",
            actor="runtime",
            subject_id=result.id,
            payload={"status": result.status.value, "member_count": len(result.members)},
        )
        return result

    @staticmethod
    def _classify_execution(
        summary: ExecutionSummary,
    ) -> tuple[BundleMemberCommitStatus, ExecutionStatus | None]:
        statuses = {outcome.status for outcome in summary.outcomes}
        if statuses == {ExecutionStatus.CONFIRMED_SUCCESS}:
            return BundleMemberCommitStatus.EXECUTED, ExecutionStatus.CONFIRMED_SUCCESS
        for status in (ExecutionStatus.UNKNOWN, ExecutionStatus.UNAVAILABLE):
            if status in statuses:
                return BundleMemberCommitStatus.UNKNOWN, status
        for status in (
            ExecutionStatus.FAILED,
            ExecutionStatus.REJECTED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.ACCEPTED,
        ):
            if status in statuses:
                return BundleMemberCommitStatus.FAILED, status
        return BundleMemberCommitStatus.FAILED, None


class BundleRecoveryService:
    """Reconciles bundle records after a crash without replaying mutations."""

    def __init__(
        self,
        *,
        bundle_repository: BundleCommitRepository,
        plan_repository: PlanRepository | None,
        scheduled_repository: ScheduledPlanRepository,
        audit: AuditLog,
    ) -> None:
        self.bundle_repository = bundle_repository
        self.plan_repository = plan_repository
        self.scheduled_repository = scheduled_repository
        self.audit = audit

    async def recover_orphaned_bundles(self) -> list[str]:
        recovered: list[str] = []
        for bundle in await self.bundle_repository.list_non_terminal():
            members = list(bundle.members)
            committed = 0
            has_unknown = False
            has_failed = False
            all_scheduled = True
            for index, member in enumerate(members):
                scheduled = await self.scheduled_repository.get(member.plan_id)
                plan = (
                    await self.plan_repository.get(member.plan_id)
                    if self.plan_repository is not None
                    else None
                )
                if scheduled is not None:
                    _scheduled_plan, schedule_status = scheduled
                    if schedule_status == "pending":
                        members[index] = member.model_copy(
                            update={"status": BundleMemberCommitStatus.SCHEDULED, "scheduled": True}
                        )
                        committed += 1
                        continue
                    if schedule_status == "executed":
                        members[index] = member.model_copy(
                            update={
                                "status": BundleMemberCommitStatus.EXECUTED,
                                "scheduled": True,
                                "execution_status": ExecutionStatus.CONFIRMED_SUCCESS,
                            }
                        )
                        committed += 1
                        all_scheduled = False
                        continue
                    has_failed = True
                    all_scheduled = False
                    members[index] = member.model_copy(
                        update={"status": BundleMemberCommitStatus.FAILED, "scheduled": True}
                    )
                    continue
                all_scheduled = False
                if plan is not None and plan.status is PlanStatus.COMPLETED:
                    members[index] = member.model_copy(
                        update={
                            "status": BundleMemberCommitStatus.EXECUTED,
                            "execution_status": ExecutionStatus.CONFIRMED_SUCCESS,
                        }
                    )
                    committed += 1
                elif plan is not None and plan.status in {
                    PlanStatus.FAILED,
                    PlanStatus.PARTIALLY_FAILED,
                    PlanStatus.CANCELLED,
                }:
                    has_failed = True
                    members[index] = member.model_copy(
                        update={"status": BundleMemberCommitStatus.FAILED}
                    )
                else:
                    has_unknown = True
                    members[index] = member.model_copy(
                        update={"status": BundleMemberCommitStatus.UNKNOWN}
                    )
            if has_unknown:
                status = (
                    BundleCommitStatus.PARTIALLY_COMMITTED
                    if committed
                    else BundleCommitStatus.UNKNOWN
                )
            elif has_failed:
                status = (
                    BundleCommitStatus.PARTIALLY_COMMITTED
                    if committed
                    else BundleCommitStatus.FAILED
                )
            elif committed == len(members):
                status = (
                    BundleCommitStatus.SCHEDULED
                    if all_scheduled
                    else BundleCommitStatus.COMPLETED
                )
            else:
                status = BundleCommitStatus.UNKNOWN
            updated = await self.bundle_repository.save(
                bundle.model_copy(update={"members": members, "status": status})
            )
            self.audit.append(
                event_type="bundle_commit_recovered",
                actor="runtime",
                subject_id=updated.id,
                payload={"status": updated.status.value, "replayed": False},
            )
            recovered.append(updated.id)
        return recovered
