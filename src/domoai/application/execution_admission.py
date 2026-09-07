"""Single server-owned admission boundary for physical execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    BundleMemberCommitStatus,
    ExecutionStatus,
    Plan,
    ValidationStatus,
)


@dataclass(frozen=True)
class AdmissionDecision:
    plan_id: str
    bundle_id: str | None
    predecessor_plan_ids: tuple[str, ...] = ()


class ExecutionAdmission:
    """Guard all entry points before the executor can claim a plan."""

    def __init__(self, *, bundle_repository: Any | None = None) -> None:
        self.bundle_repository = bundle_repository

    async def admit(self, plan: Plan, *, aggregate_owner: bool = False) -> AdmissionDecision:
        if self.bundle_repository is None:
            return AdmissionDecision(plan_id=plan.id, bundle_id=None)
        bundle = await self.bundle_repository.get_for_plan(plan.id)
        if bundle is None:
            if plan.approval is not None and plan.approval.bundle_digest is not None:
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "Bundle-scoped approval has no verifiable bundle membership",
                    details={"plan_id": plan.id},
                )
            return AdmissionDecision(plan_id=plan.id, bundle_id=None)
        member = next((item for item in bundle.members if item.plan_id == plan.id), None)
        if member is None:
            return AdmissionDecision(plan_id=plan.id, bundle_id=bundle.id)
        if not aggregate_owner:
            raise DomainError(
                ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN,
                "Bundle members must execute through the bundle aggregate",
                details={"bundle_id": bundle.id, "plan_id": plan.id},
            )
        if (
            plan.validation is not None
            and plan.validation.status is ValidationStatus.REQUIRES_CONFIRMATION
        ):
            if plan.approval is None or plan.approval.bundle_digest != bundle.bundle_digest:
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "Approval does not match the current bundle scope",
                    details={
                        "bundle_id": bundle.id,
                        "plan_id": plan.id,
                    },
                )
        predecessor_ids = tuple(member.all_predecessor_plan_ids)
        for predecessor_id in predecessor_ids:
            predecessor = next(
                (item for item in bundle.members if item.plan_id == predecessor_id), None
            )
            if (
                predecessor is None
                or predecessor.status is not BundleMemberCommitStatus.EXECUTED
                or not isinstance(predecessor.details.get("dependency_evidence"), dict)
                or predecessor.details["dependency_evidence"].get("status")
                != ExecutionStatus.CONFIRMED_SUCCESS.value
            ):
                raise DomainError(
                    ErrorCode.PRECONDITION_FAILED,
                    "All bundle predecessors must have confirmed-success evidence",
                    details={
                        "bundle_id": bundle.id,
                        "plan_id": plan.id,
                        "predecessor_plan_id": predecessor_id,
                    },
                )
        return AdmissionDecision(
            plan_id=plan.id,
            bundle_id=bundle.id,
            predecessor_plan_ids=predecessor_ids,
        )
