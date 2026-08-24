"""Command/plan validation and approval use cases."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from domoai.application.policy_engine import PolicyEngine
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    Approval,
    Capability,
    CapabilityKind,
    Command,
    DependencyKind,
    Device,
    ErrorDetail,
    ExecutionWindow,
    Plan,
    PlanDependencies,
    PlanStatus,
    PolicyAction,
    PolicyDecision,
    ValidationResult,
    ValidationStatus,
)
from domoai.domain.transitions import assert_plan_transition
from domoai.runtime.approval_store import ApprovalGrant
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executable_fingerprint import capability_fingerprint
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@dataclass(frozen=True)
class CommandValidation:
    command: Command
    capability: Capability | None
    errors: list[ErrorDetail]


class PlanService:
    DEFAULT_PLAN_TTL = timedelta(minutes=15)

    def __init__(
        self,
        registry: DeviceRegistry,
        state_store: StateStore,
        policy_engine: PolicyEngine,
        audit: AuditLog,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.registry = registry
        self.state_store = state_store
        self.policy_engine = policy_engine
        self.audit = audit
        self.clock = clock or SystemClock()

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
            created_at=self.clock.now(),
            expires_at=expires_at or self.clock.now() + self.DEFAULT_PLAN_TTL,
        )

    def normalize_command(self, command: Command) -> Command:
        result = self.validate_command_semantics(command)
        if result.errors:
            error = result.errors[0]
            raise DomainError(
                ErrorCode(error.code),
                error.message,
                field=error.field,
                device_id=error.device_id,
                capability=error.capability,
                details=error.details,
            )
        return result.command

    def validate_command_semantics(self, command: Command) -> CommandValidation:
        """Resolve and validate one command against the canonical UDM capability."""

        device = self.registry.get(command.device_id)
        if device is None:
            return CommandValidation(
                command,
                None,
                [self._error(ErrorCode.DEVICE_NOT_FOUND, "Unknown device", command)],
            )
        capability = next(
            (item for item in device.capabilities if command.command in item.commands),
            None,
        )
        if capability is None:
            return CommandValidation(
                command,
                None,
                [
                    self._error(
                        ErrorCode.UNSUPPORTED_COMMAND,
                        f"Command {command.command!r} is not supported by the device",
                        command,
                    )
                ],
            )

        errors: list[ErrorDetail] = []
        normalized = command
        if capability.unit is not None and command.unit is None:
            normalized = normalized.model_copy(update={"unit": capability.unit})
        elif command.unit != capability.unit:
            errors.append(
                self._make_error(
                    ErrorCode.INVALID_CAPABILITY,
                    f"Command unit {command.unit!r} does not match capability unit "
                    f"{capability.unit!r}",
                    command,
                    field="unit",
                    details={"expected_unit": capability.unit, "received_unit": command.unit},
                )
            )
        if not capability.writable:
            errors.append(
                self._make_error(
                    ErrorCode.INVALID_CAPABILITY,
                    "Command targets a read-only capability",
                    command,
                    field="writable",
                    details={"writable": False},
                )
            )
        errors.extend(self._validate_value(normalized, capability))
        return CommandValidation(normalized, capability, errors)

    def capability_for_command(self, command: Command) -> Capability | None:
        device = self.registry.get(command.device_id)
        return (
            self._find_command_capability(device, command.command)
            if device is not None
            else None
        )

    def validate_command_value(self, command: Command) -> list[ErrorDetail]:
        """Recheck the live capability envelope immediately before execution."""

        return self.validate_command_semantics(command).errors

    def validate(self, plan: Plan) -> Plan:
        validated_at = self.clock.now()
        validation_expires_at = plan.expires_at or self._default_validation_expiry(
            plan, validated_at
        )
        temporal_plan = plan
        if plan.execute_at is not None and plan.execution_window is None:
            timezone = getattr(plan.execute_at.tzinfo, "key", None) or plan.execute_at.tzname()
            temporal_plan = plan.model_copy(
                update={
                    "execution_window": ExecutionWindow(
                        intended_at=plan.execute_at,
                        not_before=plan.execute_at,
                        not_after=plan.expires_at
                        or plan.execute_at + self.DEFAULT_PLAN_TTL,
                        timezone=timezone or "UTC",
                        revision=plan.schedule_revision,
                    )
                }
            )
        elif (
            plan.execution_window is not None
            and plan.execution_window.not_after > validation_expires_at
        ):
            validation_expires_at = plan.execution_window.not_after
        errors = []
        decisions: list[PolicyDecision] = []
        seen_idempotency_keys: set[str] = set()
        state_versions: dict[str, int] = {}
        dependency_kinds: dict[str, DependencyKind] = {}
        capability_fingerprints: dict[str, str] = {}
        normalized_commands: list[Command] = []
        for command in plan.commands:
            semantic = self.validate_command_semantics(command)
            command = semantic.command
            normalized_commands.append(command)
            errors.extend(semantic.errors)
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
                errors.append(self._error(ErrorCode.DEVICE_NOT_FOUND, "Unknown device", command))
                continue
            capability = semantic.capability
            if capability is None:
                continue
            for precondition in command.preconditions:
                state_key = f"{precondition.device_id}::{precondition.capability}"
                state_versions[state_key] = self.state_store.state_version(
                    precondition.device_id, precondition.capability
                )
                dependency_kinds[state_key] = DependencyKind.PRECONDITION
            capability_fingerprints[f"{device.id}::{command.command}"] = capability_fingerprint(
                device,
                capability,
                self.registry.routes_for(device.id, capability.name),
            )
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
            errors.extend(self._validate_postconditions(command, device))
            for postcondition in command.postconditions:
                feedback_capability = next(
                    (
                        item
                        for item in device.capabilities
                        if item.name == postcondition.capability and item.readable
                    ),
                    None,
                )
                feedback_routes = self.registry.routes_for(
                    device.id, postcondition.capability
                )
                if feedback_capability is not None and len(
                    [route for route in feedback_routes if route.available]
                ) == 1:
                    capability_fingerprints[
                        f"{device.id}::postcondition::{postcondition.capability}"
                    ] = capability_fingerprint(device, feedback_capability, feedback_routes)
                for reconcile_capability_name in postcondition.reconcile_capabilities:
                    reconcile_capability = next(
                        (
                            item
                            for item in device.capabilities
                            if item.name == reconcile_capability_name and item.readable
                        ),
                        None,
                    )
                    reconcile_routes = self.registry.routes_for(
                        device.id, reconcile_capability_name
                    )
                    available_reconcile_routes = [
                        route for route in reconcile_routes if route.available
                    ]
                    if reconcile_capability is not None and len(available_reconcile_routes) == 1:
                        fingerprint_key = (
                            f"{device.id}::reconcile::{reconcile_capability_name}"
                        )
                        capability_fingerprints[fingerprint_key] = capability_fingerprint(
                            device, reconcile_capability, reconcile_routes
                        )
            decision = self.policy_engine.evaluate(command, device, capability.name)
            decisions.append(decision)
            if decision.action is PolicyAction.DENY:
                errors.append(self._error(ErrorCode.POLICY_DENIED, decision.reason, command))

        revision = self.current_revision
        dependencies = PlanDependencies(
            inventory_revision=self.state_store.runtime_revision,
            policy_revision=self.policy_engine.revision,
            state_versions=state_versions,
            dependency_kinds=dependency_kinds,
            capability_fingerprints=capability_fingerprints,
        )
        validated_plan = temporal_plan.model_copy(update={"commands": normalized_commands})
        definition_digest = self._definition_digest(validated_plan)
        digest = self._digest(validated_plan, revision, decisions, dependencies)
        requires_confirmation = any(
            decision.action is PolicyAction.CONFIRM for decision in decisions
        )
        status = (
            ValidationStatus.INVALID
            if errors
            else (
                ValidationStatus.REQUIRES_CONFIRMATION
                if requires_confirmation
                else ValidationStatus.VALID
            )
        )
        plan_status = (
            PlanStatus.VALIDATED
            if errors
            else (PlanStatus.REQUIRES_CONFIRMATION if requires_confirmation else PlanStatus.READY)
        )
        validation = ValidationResult(
            status=status,
            validated_at=validated_at,
            runtime_revision=revision,
            errors=errors,
            digest=digest,
            dependencies=dependencies,
        )
        updated = validated_plan.model_copy(
            update={
                "status": plan_status,
                "definition_digest": definition_digest,
                "validation": validation,
                "policy_decisions": decisions,
                "expires_at": validation_expires_at,
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

    @classmethod
    def _default_validation_expiry(cls, plan: Plan, validated_at: datetime) -> datetime:
        """Keep scheduled plans valid through their requested execution window."""

        anchor = (
            plan.execute_at
            if plan.execute_at is not None and plan.execute_at > validated_at
            else validated_at
        )
        return anchor + cls.DEFAULT_PLAN_TTL

    def approve(self, plan: Plan, *, grant: ApprovalGrant) -> Plan:
        if plan.status is not PlanStatus.REQUIRES_CONFIRMATION or plan.validation is None:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Only a validated plan requiring confirmation can be approved",
            )
        if grant.plan_id != plan.id or grant.validation_digest != plan.validation.digest:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval grant does not match the validated plan",
            )
        expected_window_digest = plan.execution_window.digest if plan.execution_window else None
        if (
            grant.window_digest != expected_window_digest
            or grant.schedule_revision != plan.schedule_revision
        ):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval grant does not match the plan execution window",
            )
        assert_plan_transition(plan.status, PlanStatus.APPROVED)
        approval = Approval(
            status="approved",
            approved_by=grant.approved_by,
            approved_at=grant.issued_at,
            validation_digest=plan.validation.digest,
            authentication_context=grant.authentication_context,
            session_id=grant.session_id,
            window_digest=expected_window_digest,
            schedule_revision=plan.schedule_revision,
        )
        approved = plan.model_copy(update={"status": PlanStatus.APPROVED, "approval": approval})
        self.audit.append(
            event_type="plan_approved",
            actor=grant.approved_by,
            subject_id=plan.id,
            payload={
                "validation_digest": approval.validation_digest,
                "approval_id": grant.approval_id,
                "authentication_context": grant.authentication_context,
                "session_id": grant.session_id,
                "window_digest": approval.window_digest,
                "schedule_revision": approval.schedule_revision,
            },
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

    def assert_executable(
        self, plan: Plan, *, state_version_overrides: dict[str, int] | None = None
    ) -> None:
        if plan.status is PlanStatus.CANCELLED:
            raise DomainError(ErrorCode.INVALID_TRANSITION, "Plan is cancelled and cannot execute")
        if plan.expires_at is not None and plan.expires_at <= self.clock.now():
            raise DomainError(
                ErrorCode.STALE_PLAN,
                "Plan has expired; create and validate a new plan",
            )
        if plan.validation is None:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "Plan has not been validated")
        if plan.validation.status is ValidationStatus.INVALID:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "Plan validation failed")
        if plan.status not in {PlanStatus.READY, PlanStatus.APPROVED}:
            raise DomainError(
                ErrorCode.INVALID_TRANSITION,
                "Only ready or approved plans can execute",
            )
        if not self._dependencies_still_current(
            plan, plan.validation, state_version_overrides=state_version_overrides
        ):
            raise DomainError(
                ErrorCode.STALE_PLAN,
                "Plan runtime revision is stale; revalidate before execution",
            )
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

    def _dependencies_still_current(
        self,
        plan: Plan,
        validation: ValidationResult,
        *,
        state_version_overrides: dict[str, int] | None = None,
    ) -> bool:
        dependencies = validation.dependencies
        if dependencies is None:
            return False
        if dependencies.inventory_revision != self.state_store.runtime_revision:
            return False
        if dependencies.policy_revision != self.policy_engine.revision:
            return False
        if not dependencies.capability_fingerprints:
            return False
        current_capability_fingerprints: dict[str, str] = {}
        for command in plan.commands:
            device = self.registry.get(command.device_id)
            if device is None:
                return False
            capability = self._find_capability(device, command.command)
            if capability is None:
                return False
            current_capability_fingerprints[f"{device.id}::{command.command}"] = (
                capability_fingerprint(
                    device,
                    capability,
                    self.registry.routes_for(device.id, capability.name),
                )
            )
            for postcondition in command.postconditions:
                feedback_capability = next(
                    (
                        item
                        for item in device.capabilities
                        if item.name == postcondition.capability and item.readable
                    ),
                    None,
                )
                feedback_routes = self.registry.routes_for(
                    device.id, postcondition.capability
                )
                if feedback_capability is None or len(
                    [route for route in feedback_routes if route.available]
                ) != 1:
                    return False
                current_capability_fingerprints[
                    f"{device.id}::postcondition::{postcondition.capability}"
                ] = capability_fingerprint(device, feedback_capability, feedback_routes)
                for reconcile_capability_name in postcondition.reconcile_capabilities:
                    reconcile_capability = next(
                        (
                            item
                            for item in device.capabilities
                            if item.name == reconcile_capability_name and item.readable
                        ),
                        None,
                    )
                    reconcile_routes = self.registry.routes_for(
                        device.id, reconcile_capability_name
                    )
                    available_reconcile_routes = [
                        route for route in reconcile_routes if route.available
                    ]
                    if reconcile_capability is None or len(available_reconcile_routes) != 1:
                        return False
                    key = f"{device.id}::reconcile::{reconcile_capability_name}"
                    current_capability_fingerprints[key] = capability_fingerprint(
                        device, reconcile_capability, reconcile_routes
                    )
        if current_capability_fingerprints != dependencies.capability_fingerprints:
            return False
        for key, version in dependencies.state_versions.items():
            device_id, _, capability_name = key.partition("::")
            expected_version = (
                state_version_overrides.get(key, version)
                if state_version_overrides is not None
                else version
            )
            if self.state_store.state_version(device_id, capability_name) != expected_version:
                return False
        return True

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
    def _find_command_capability(device: Device, command_name: str) -> Capability | None:
        return next(
            (
                capability
                for capability in device.capabilities
                if command_name in capability.commands
            ),
            None,
        )

    def _validate_postconditions(self, command: Command, device: Device) -> list[ErrorDetail]:
        errors: list[ErrorDetail] = []
        for postcondition in command.postconditions:
            capability = next(
                (item for item in device.capabilities if item.name == postcondition.capability),
                None,
            )
            if capability is None or not capability.readable:
                errors.append(
                    self._make_error(
                        ErrorCode.INVALID_CAPABILITY,
                        f"Postcondition capability {postcondition.capability!r} is not readable",
                        command,
                    )
                )
                continue
            routes = self.registry.routes_for(device.id, postcondition.capability)
            available_routes = [route for route in routes if route.available]
            if len(available_routes) > 1:
                errors.append(
                    self._make_error(
                        ErrorCode.ROUTE_AMBIGUOUS,
                        f"Postcondition capability {postcondition.capability!r} has "
                        "multiple available routes",
                        command,
                    )
                )
            elif not available_routes:
                errors.append(
                    self._make_error(
                        ErrorCode.ROUTE_NOT_FOUND if not routes else ErrorCode.SOURCE_UNAVAILABLE,
                        f"Postcondition capability {postcondition.capability!r} has no "
                        "available source route",
                        command,
                    )
                )
            for reconcile_capability_name in postcondition.reconcile_capabilities:
                capability = next(
                    (
                        item
                        for item in device.capabilities
                        if item.name == reconcile_capability_name
                    ),
                    None,
                )
                if capability is None or not capability.readable:
                    errors.append(
                        self._make_error(
                            ErrorCode.INVALID_CAPABILITY,
                            f"Reconciliation capability {reconcile_capability_name!r} "
                            "is not readable",
                            command,
                        )
                    )
                    continue
                routes = self.registry.routes_for(device.id, reconcile_capability_name)
                available_routes = [route for route in routes if route.available]
                if len(available_routes) > 1:
                    errors.append(
                        self._make_error(
                            ErrorCode.ROUTE_AMBIGUOUS,
                            f"Reconciliation capability {reconcile_capability_name!r} "
                            "has multiple available routes",
                            command,
                        )
                    )
                elif not available_routes:
                    errors.append(
                        self._make_error(
                            (
                                ErrorCode.ROUTE_NOT_FOUND
                                if not routes
                                else ErrorCode.SOURCE_UNAVAILABLE
                            ),
                            f"Reconciliation capability {reconcile_capability_name!r} "
                            "has no available source route",
                            command,
                        )
                    )
        return errors

    @staticmethod
    def _validate_value(command: Command, capability: Capability) -> list[ErrorDetail]:
        if command.value is None:
            return []
        value = command.value
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        numeric_value = float(value) if numeric else None
        errors: list[ErrorDetail] = []
        if capability.kind is CapabilityKind.BOOLEAN and not isinstance(value, bool):
            errors.append(
                PlanService._make_error(
                    ErrorCode.INVALID_COMMAND_VALUE,
                    "Boolean capability requires a boolean value",
                    command,
                    field="value",
                    details={"expected_kind": capability.kind.value},
                )
            )
        elif capability.kind is CapabilityKind.INTEGER and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            errors.append(
                PlanService._make_error(
                    ErrorCode.INVALID_COMMAND_VALUE,
                    "Integer capability requires an integer value",
                    command,
                    field="value",
                    details={"expected_kind": capability.kind.value},
                )
            )
        elif capability.kind is CapabilityKind.NUMBER and not numeric:
            errors.append(
                PlanService._make_error(
                    ErrorCode.INVALID_COMMAND_VALUE,
                    "Number capability requires a numeric value",
                    command,
                    field="value",
                    details={"expected_kind": capability.kind.value},
                )
            )
        elif capability.kind in {CapabilityKind.TEXT, CapabilityKind.TIMESTAMP} and not isinstance(
            value, str
        ):
            errors.append(
                PlanService._make_error(
                    ErrorCode.INVALID_COMMAND_VALUE,
                    "Textual capability requires a string value",
                    command,
                    field="value",
                    details={"expected_kind": capability.kind.value},
                )
            )
        elif capability.kind is CapabilityKind.ENUM and not isinstance(value, str):
            errors.append(
                PlanService._make_error(
                    ErrorCode.INVALID_COMMAND_VALUE,
                    "Enum capability requires a string value",
                    command,
                    field="value",
                    details={"expected_kind": capability.kind.value},
                )
            )
        if numeric and not math.isfinite(float(value)):
            errors.append(
                PlanService._make_error(
                    ErrorCode.INVALID_COMMAND_VALUE,
                    "Numeric command values must be finite",
                    command,
                    field="value",
                )
            )
        if (
            numeric_value is not None
            and capability.minimum is not None
            and numeric_value < capability.minimum
        ):
            errors.append(
                PlanService._make_error(
                    ErrorCode.VALUE_OUT_OF_RANGE,
                    f"Value must be greater than or equal to {capability.minimum}",
                    command,
                    field="value",
                    details={"minimum": capability.minimum},
                )
            )
        if (
            numeric_value is not None
            and capability.maximum is not None
            and numeric_value > capability.maximum
        ):
            errors.append(
                PlanService._make_error(
                    ErrorCode.VALUE_OUT_OF_RANGE,
                    f"Value must be less than or equal to {capability.maximum}",
                    command,
                    field="value",
                    details={"maximum": capability.maximum},
                )
            )
        if numeric_value is not None and (step := capability.constraints.get("step")) is not None:
            base = capability.minimum if capability.minimum is not None else 0
            quotient = (numeric_value - float(base)) / float(step)
            if not math.isclose(quotient, round(quotient), rel_tol=0.0, abs_tol=1e-9):
                errors.append(
                    PlanService._make_error(
                        ErrorCode.INVALID_COMMAND_VALUE,
                        f"Value must align to step {step}",
                        command,
                        field="value",
                        details={"step": step, "base": base},
                    )
                )
        if capability.enum_values and (
            not isinstance(value, str) or value not in capability.enum_values
        ):
            errors.append(
                PlanService._make_error(
                    ErrorCode.INVALID_COMMAND_VALUE,
                    "Value is not in the capability enum",
                    command,
                    field="value",
                    details={"allowed_values": capability.enum_values},
                )
            )
        return errors

    @staticmethod
    def _error(code: ErrorCode, message: str, command: Command) -> ErrorDetail:
        return PlanService._make_error(code, message, command)

    @staticmethod
    def _make_error(
        code: ErrorCode,
        message: str,
        command: Command,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ErrorDetail:
        return ErrorDetail(
            code=code,
            message=message,
            field=field,
            device_id=command.device_id,
            retryable=False,
            details=details or {},
        )

    @staticmethod
    def _definition_digest(plan: Plan) -> str:
        payload: dict[str, Any] = {
            "plan_id": plan.id,
            "schema_version": plan.schema_version,
            "execute_at": plan.execute_at.isoformat() if plan.execute_at else None,
            "execution_window": (
                plan.execution_window.canonical_payload() if plan.execution_window else None
            ),
            "schedule_revision": plan.schedule_revision,
            "commands": [command.model_dump(mode="json") for command in plan.commands],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    @staticmethod
    def _digest(
        plan: Plan,
        revision: str,
        decisions: list[PolicyDecision],
        dependencies: PlanDependencies,
    ) -> str:
        payload: dict[str, Any] = {
            "plan_id": plan.id,
            # Validation lifetime is a runtime admission-control field, not
            # the semantic command intent. Keeping it outside the digest
            # makes a plan validated by optimizer preview and then by MCP
            # retain the same evidence identity while each validation still
            # enforces its own durable expiry.
            "commands": [command.model_dump(mode="json") for command in plan.commands],
            "execution_window": (
                plan.execution_window.canonical_payload() if plan.execution_window else None
            ),
            "schedule_revision": plan.schedule_revision,
            "runtime_revision": revision,
            "dependencies": dependencies.model_dump(mode="json"),
            "policy_decisions": [decision.model_dump(mode="json") for decision in decisions],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
