"""Command/plan validation and approval use cases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    Approval,
    Capability,
    Command,
    Device,
    ErrorDetail,
    Plan,
    PlanStatus,
    PolicyAction,
    PolicyDecision,
    ValidationResult,
    ValidationStatus,
)
from domoai.domain.transitions import assert_plan_transition
from domoai.runtime.events import AuditLog
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


class PlanService:
    DEFAULT_PLAN_TTL = timedelta(minutes=15)

    def __init__(
        self,
        registry: DeviceRegistry,
        state_store: StateStore,
        policy_engine: PolicyEngine,
        audit: AuditLog,
    ) -> None:
        self.registry = registry
        self.state_store = state_store
        self.policy_engine = policy_engine
        self.audit = audit

    @property
    def current_revision(self) -> str:
        return f"{self.state_store.runtime_revision}:{self.policy_engine.revision}"

    def create_plan(
        self,
        plan_id: str,
        commands: Sequence[Command],
        *,
        expires_at: datetime | None = None,
    ) -> Plan:
        normalized = [self.normalize_command(command) for command in commands]
        return Plan(
            id=plan_id,
            commands=normalized,
            expires_at=expires_at or datetime.now(UTC) + self.DEFAULT_PLAN_TTL,
        )

    def normalize_command(self, command: Command) -> Command:
        device = self.registry.get(command.device_id)
        if device is None:
            raise DomainError(ErrorCode.DEVICE_NOT_FOUND, "Unknown device")
        capability = self._find_capability(device, command.command)
        if capability is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_COMMAND,
                f"Command {command.command!r} is not supported by the device",
            )
        if (
            command.unit is not None
            and capability.unit is not None
            and command.unit != capability.unit
        ):
            raise DomainError(
                ErrorCode.INVALID_CAPABILITY,
                f"Command unit {command.unit!r} does not match capability unit {capability.unit!r}",
            )
        return command.model_copy(
            update={"unit": command.unit or capability.unit}
        )

    def capability_for_command(self, command: Command) -> Capability | None:
        device = self.registry.get(command.device_id)
        return self._find_capability(device, command.command) if device is not None else None

    def validate(self, plan: Plan) -> Plan:
        errors = []
        decisions: list[PolicyDecision] = []
        seen_idempotency_keys: set[str] = set()
        for command in plan.commands:
            if command.idempotency_key in seen_idempotency_keys:
                errors.append(
                    self._error(
                        ErrorCode.DUPLICATE_COMMAND,
                        "Each command must have a unique idempotency key within a plan",
                        command,
                    )
                )
            seen_idempotency_keys.add(command.idempotency_key)
            device = self.registry.get(command.device_id)
            if device is None:
                errors.append(
                    self._error(ErrorCode.DEVICE_NOT_FOUND, "Unknown device", command)
                )
                continue
            capability = self._find_capability(device, command.command)
            if capability is None:
                errors.append(
                    self._error(
                        ErrorCode.UNSUPPORTED_COMMAND,
                        f"Command {command.command!r} is not supported by the device",
                        command,
                    )
                )
                continue
            errors.extend(self._validate_value(command, capability))
            route = self.registry.resolve_command_route(device.id, command.command)
            if route.reason is not None:
                route_code = {
                    "ambiguous_route": ErrorCode.ROUTE_AMBIGUOUS,
                    "route_not_found": ErrorCode.ROUTE_NOT_FOUND,
                    "source_unavailable": ErrorCode.SOURCE_UNAVAILABLE,
                }.get(route.reason)
                if route_code is not None:
                    errors.append(
                        self._error(
                            route_code,
                            f"No unique available source route for command {command.command!r}",
                            command,
                        )
                    )
            decision = self.policy_engine.evaluate(command, device, capability.name)
            decisions.append(decision)
            if decision.action is PolicyAction.DENY:
                errors.append(
                    self._error(ErrorCode.POLICY_DENIED, decision.reason, command)
                )

        revision = self.current_revision
        digest = self._digest(plan, revision, decisions)
        requires_confirmation = any(
            decision.action is PolicyAction.CONFIRM for decision in decisions
        )
        status = ValidationStatus.INVALID if errors else (
            ValidationStatus.REQUIRES_CONFIRMATION
            if requires_confirmation
            else ValidationStatus.VALID
        )
        plan_status = PlanStatus.VALIDATED if errors else (
            PlanStatus.REQUIRES_CONFIRMATION if requires_confirmation else PlanStatus.READY
        )
        validation = ValidationResult(
            status=status,
            validated_at=datetime.now(UTC),
            runtime_revision=revision,
            errors=errors,
            digest=digest,
        )
        updated = plan.model_copy(
            update={
                "status": plan_status,
                "validation": validation,
                "policy_decisions": decisions,
            }
        )
        self.audit.append(
            event_type="plan_validated",
            actor="runtime",
            subject_id=plan.id,
            payload={
                "status": validation.status.value,
                "error_count": len(errors),
                "requires_confirmation": requires_confirmation,
            },
        )
        return updated

    def approve(self, plan: Plan, *, approved_by: str) -> Plan:
        if plan.status is not PlanStatus.REQUIRES_CONFIRMATION or plan.validation is None:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Only a validated plan requiring confirmation can be approved",
            )
        assert_plan_transition(plan.status, PlanStatus.APPROVED)
        approval = Approval(
            status="approved",
            approved_by=approved_by,
            approved_at=datetime.now(UTC),
            validation_digest=plan.validation.digest,
        )
        approved = plan.model_copy(update={"status": PlanStatus.APPROVED, "approval": approval})
        self.audit.append(
            event_type="plan_approved",
            actor=approved_by,
            subject_id=plan.id,
            payload={"validation_digest": approval.validation_digest},
        )
        return approved

    def cancel(self, plan: Plan, *, cancelled_by: str = "operator") -> Plan:
        assert_plan_transition(plan.status, PlanStatus.CANCELLED)
        cancelled = plan.model_copy(update={"status": PlanStatus.CANCELLED})
        self.audit.append(
            event_type="plan_cancelled",
            actor=cancelled_by,
            subject_id=plan.id,
            payload={"previous_status": plan.status.value},
        )
        return cancelled

    def assert_executable(self, plan: Plan) -> None:
        if plan.status is PlanStatus.CANCELLED:
            raise DomainError(ErrorCode.INVALID_TRANSITION, "Plan is cancelled and cannot execute")
        if plan.expires_at is not None and plan.expires_at <= datetime.now(UTC):
            raise DomainError(
                ErrorCode.STALE_PLAN,
                "Plan has expired; create and validate a new plan",
            )
        if plan.validation is None:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "Plan has not been validated")
        if plan.validation.runtime_revision != self.current_revision:
            raise DomainError(
                ErrorCode.STALE_PLAN,
                "Plan runtime revision is stale; revalidate before execution",
            )
        if plan.validation.status is ValidationStatus.INVALID:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "Plan validation failed")
        if plan.validation.status is ValidationStatus.REQUIRES_CONFIRMATION:
            if plan.approval is None or plan.approval.status != "approved":
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "Plan requires explicit operator approval",
                )
            if plan.approval.validation_digest != plan.validation.digest:
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "Approval does not match the validated plan",
                )

    @staticmethod
    def _find_capability(device: Device, command_name: str) -> Capability | None:
        return next(
            (
                capability
                for capability in device.capabilities
                if capability.writable and command_name in capability.commands
            ),
            None,
        )

    @staticmethod
    def _validate_value(command: Command, capability: Capability) -> list[ErrorDetail]:
        if command.value is None:
            return []
        if capability.minimum is not None and capability.maximum is not None and (
            not isinstance(command.value, (int, float))
            or isinstance(command.value, bool)
            or command.value < capability.minimum
            or command.value > capability.maximum
        ):
            return [
                PlanService._make_error(
                    ErrorCode.VALUE_OUT_OF_RANGE,
                    f"Value must be between {capability.minimum} and {capability.maximum}",
                    command,
                )
            ]
        if capability.enum_values and str(command.value) not in capability.enum_values:
            return [
                PlanService._make_error(
                    ErrorCode.VALUE_OUT_OF_RANGE,
                    "Value is not in the capability enum",
                    command,
                )
            ]
        return []

    @staticmethod
    def _error(code: ErrorCode, message: str, command: Command) -> ErrorDetail:
        return PlanService._make_error(code, message, command)

    @staticmethod
    def _make_error(code: ErrorCode, message: str, command: Command) -> ErrorDetail:
        return ErrorDetail(
            code=code,
            message=message,
            device_id=command.device_id,
            retryable=False,
        )

    @staticmethod
    def _digest(plan: Plan, revision: str, decisions: list[PolicyDecision]) -> str:
        payload: dict[str, Any] = {
            "plan_id": plan.id,
            "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
            "commands": [command.model_dump(mode="json") for command in plan.commands],
            "runtime_revision": revision,
            "policy_decisions": [decision.model_dump(mode="json") for decision in decisions],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
