"""Single server-owned admission boundary for physical execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    BundleMemberCommitStatus,
    ExecutionStatus,
    Plan,
    PlanStatus,
)
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.events import AuditLog, redact_payload

_AUDIT_IDENTIFIER_MAX_LENGTH = 200
_AUDIT_IDENTIFIER_MAX_JSON_BYTES = _AUDIT_IDENTIFIER_MAX_LENGTH + 2
_AUDIT_IDENTIFIER_TRUNCATION_MARKER = "...[truncated]"


def _serialized_json_bytes(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def _bounded_audit_identifier(identifier: str) -> str:
    identifier = cast(str, redact_payload(identifier))
    if (
        len(identifier) <= _AUDIT_IDENTIFIER_MAX_LENGTH
        and _serialized_json_bytes(identifier) <= _AUDIT_IDENTIFIER_MAX_JSON_BYTES
    ):
        return identifier

    marker = _AUDIT_IDENTIFIER_TRUNCATION_MARKER
    lower = 0
    upper = min(len(identifier), _AUDIT_IDENTIFIER_MAX_LENGTH - len(marker))
    best = marker
    while lower <= upper:
        prefix_length = (lower + upper) // 2
        candidate = identifier[:prefix_length] + marker
        if _serialized_json_bytes(candidate) <= _AUDIT_IDENTIFIER_MAX_JSON_BYTES:
            best = candidate
            lower = prefix_length + 1
        else:
            upper = prefix_length - 1
    return best


class AdmissionOperation(StrEnum):
    EXECUTE = "execute"
    SCHEDULE = "schedule"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"


@dataclass(frozen=True)
class AdmissionDecision:
    plan_id: str
    bundle_id: str | None
    predecessor_plan_ids: tuple[str, ...] = ()


class ExecutionAdmission:
    """Guard all entry points before the executor can claim a plan."""

    _MEMBER_REJECTION_ERRORS = {
        AdmissionOperation.EXECUTE: ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN,
        AdmissionOperation.SCHEDULE: ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN,
        AdmissionOperation.CANCEL: ErrorCode.BUNDLE_MEMBER_CANCEL_FORBIDDEN,
        AdmissionOperation.RESCHEDULE: ErrorCode.BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN,
    }
    _MEMBER_REJECTION_REASON = "Bundle members must execute through the bundle aggregate"

    def __init__(
        self,
        *,
        bundle_repository: Any | None = None,
        approval_store: ApprovalStore | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.bundle_repository = bundle_repository
        self.approval_store = approval_store
        self.audit = audit

    async def admit(
        self,
        plan: Plan,
        *,
        operation: AdmissionOperation = AdmissionOperation.EXECUTE,
        aggregate_owner: bool = False,
    ) -> AdmissionDecision:
        if not isinstance(operation, AdmissionOperation):
            raise ValueError("Unsupported admission operation")
        if self.bundle_repository is None:
            self._verify_non_member_execution(
                plan,
                operation=operation,
                bundle_persistence_available=False,
            )
            return AdmissionDecision(plan_id=plan.id, bundle_id=None)
        bundle = await self.bundle_repository.get_for_plan(plan.id)
        if bundle is None:
            self._verify_non_member_execution(
                plan,
                operation=operation,
                bundle_persistence_available=True,
            )
            return AdmissionDecision(plan_id=plan.id, bundle_id=None)
        member = next((item for item in bundle.members if item.plan_id == plan.id), None)
        if member is None:
            self._verify_execution(plan, operation=operation, expected_bundle_digest=None)
            return AdmissionDecision(plan_id=plan.id, bundle_id=bundle.id)
        if not aggregate_owner:
            self._reject_bundle_member(plan, bundle_id=bundle.id, operation=operation)
        if operation is not AdmissionOperation.EXECUTE:
            return AdmissionDecision(plan_id=plan.id, bundle_id=bundle.id)
        if plan.approval is not None and (
            "bundle" not in plan.approval.scope.split("+")
            or plan.approval.bundle_digest != bundle.bundle_digest
        ):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval scope does not match the committed bundle",
                details={
                    "bundle_id": bundle.id,
                    "expected_bundle_digest": bundle.bundle_digest,
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
        expected_bundle_digest = bundle.bundle_digest if member is not None else None
        self._verify_execution(
            plan,
            operation=operation,
            expected_bundle_digest=expected_bundle_digest,
        )
        return AdmissionDecision(
            plan_id=plan.id,
            bundle_id=bundle.id,
            predecessor_plan_ids=predecessor_ids,
        )

    def _verify_non_member_execution(
        self,
        plan: Plan,
        *,
        operation: AdmissionOperation,
        bundle_persistence_available: bool,
    ) -> None:
        if operation is not AdmissionOperation.EXECUTE:
            return
        if plan.approval is not None and (
            plan.approval.bundle_digest is not None or "bundle" in plan.approval.scope.split("+")
        ):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                (
                    "A bundle-scoped approval cannot execute outside its bundle"
                    if bundle_persistence_available
                    else "Bundle-scoped approval cannot be verified without bundle persistence"
                ),
                details={"plan_id": plan.id},
            )
        self._verify_approval(plan, expected_bundle_digest=None)

    def _verify_execution(
        self,
        plan: Plan,
        *,
        operation: AdmissionOperation,
        expected_bundle_digest: str | None,
    ) -> None:
        if operation is AdmissionOperation.EXECUTE:
            self._verify_approval(plan, expected_bundle_digest=expected_bundle_digest)

    def _reject_bundle_member(
        self,
        plan: Plan,
        *,
        bundle_id: str,
        operation: AdmissionOperation,
    ) -> None:
        error_code = self._MEMBER_REJECTION_ERRORS[operation]
        if self.audit is not None:
            self.audit.append(
                event_type="execution_admission_rejected",
                actor="runtime",
                subject_id=_bounded_audit_identifier(plan.id),
                payload={
                    "operation": operation.value,
                    "plan_id": _bounded_audit_identifier(plan.id),
                    "bundle_id": _bounded_audit_identifier(bundle_id),
                    "error_code": error_code.value,
                    "reason": self._MEMBER_REJECTION_REASON,
                },
            )
        raise DomainError(
            error_code,
            self._MEMBER_REJECTION_REASON,
            details={"bundle_id": bundle_id, "plan_id": plan.id},
        )

    def _verify_approval(self, plan: Plan, *, expected_bundle_digest: str | None) -> None:
        if plan.status is not PlanStatus.APPROVED:
            return
        if self.approval_store is None:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approved physical execution requires the authoritative approval store",
            )
        self.approval_store.verify_consumed(
            plan,
            bundle_digest=expected_bundle_digest,
        )
