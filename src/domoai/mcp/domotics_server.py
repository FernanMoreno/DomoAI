"""Semantic MCP v1 server backed by the shared application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from domoai.application.authority import AuthorityPolicy
from domoai.application.bundle_commit import BundleCommitRequest, BundleCommitService
from domoai.application.commissioning import (
    CommissioningQualificationRepository,
    CommissioningService,
)
from domoai.application.discovery_service import DiscoveryService
from domoai.application.execution_admission import AdmissionOperation
from domoai.application.facade import DomoticsFacade
from domoai.application.local_automation import LocalAutomationEngine
from domoai.application.metrics import RuntimeMetricsCollector
from domoai.application.optimization_worker import OptimizationWorker, WorkerOperationError
from domoai.application.recurrence import recurrence_digest
from domoai.application.scheduler import Scheduler
from domoai.application.state_service import StateService
from domoai.domain.automation import (
    AutomationConsent,
    AutomationRule,
    AutomationRuleStatus,
    automation_rule_digest,
)
from domoai.domain.commissioning import CommissioningEvidence, CommissioningReport
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import Command, DeviceType, Plan, PlanStatus, Policy, RecurrenceRule
from domoai.domain.privacy import HouseholdDataPolicy
from domoai.mcp.auth import (
    current_access_token,
    current_authority,
    current_client_id,
    require_client_scope,
)
from domoai.mcp.compat import ensure_fastmcp_settings_ready
from domoai.mcp.errors import error_envelope
from domoai.mcp.request_context import with_request_principal
from domoai.mcp.resources import (
    as_json,
    capabilities_snapshot,
    coverage_snapshot,
    energy_context_snapshot,
    energy_snapshot,
    inventory_snapshot,
    policies_snapshot,
    runtime_snapshot,
)
from domoai.optimizer.energy import EnergyContext
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.ports import EnergyContextProvider
from domoai.optimizer.providers import EnergyProviderError
from domoai.persistence.repositories import (
    AuditEventRepository,
    PlanRepository,
    StateHistoryRepository,
)
from domoai.runtime.approval_store import (
    ApprovalStore,
    OperatorApprovalAssertionProvider,
    OperatorPrincipalProvider,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.registry import DeviceRegistry


@dataclass
class DomoticsMcpContext:
    discovery: DiscoveryService
    state_service: StateService
    facade: DomoticsFacade
    registry: DeviceRegistry
    policies: list[Policy]
    active_provider_ids: tuple[str, ...] = ()
    battery_qualification: str = "unsupported"
    plan_repository: PlanRepository | None = None
    state_history_repository: StateHistoryRepository | None = None
    approval_store: ApprovalStore = field(default_factory=ApprovalStore)
    energy_context_provider: EnergyContextProvider | None = None
    plans: dict[str, Plan] = field(default_factory=dict)
    last_refreshed_at: datetime | None = None
    scheduler: Scheduler | None = None
    audit_repository: AuditEventRepository | None = None
    metrics: RuntimeMetricsCollector | None = None
    bundle_commit_service: BundleCommitService | None = None
    operator_principal_provider: OperatorPrincipalProvider | None = None
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None
    blocking_worker: OptimizationWorker | None = None
    provider_timeout_seconds: float = 10.0
    clock: Clock = field(default_factory=SystemClock)
    commissioning_service: CommissioningService | None = None
    commissioning_report: CommissioningReport | None = None
    local_automation: LocalAutomationEngine | None = None
    privacy_service: PrivacyService | None = None
    privacy_policy: HouseholdDataPolicy | None = None
    qualification_repository: CommissioningQualificationRepository | None = None
    authority_policy: AuthorityPolicy = field(default_factory=AuthorityPolicy)


def _parse_timezone_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _require_mutation_scope() -> None:
    require_client_scope(current_access_token(), "mutate")


def _authority_policy(context: DomoticsMcpContext) -> AuthorityPolicy | None:
    policy = getattr(context, "authority_policy", None)
    return policy if isinstance(policy, AuthorityPolicy) else None


def _authorize_mutation(context: DomoticsMcpContext, *, operation: str, subject_id: str) -> None:
    authority = current_authority()
    try:
        _require_mutation_scope()
    except DomainError as error:
        client_id = current_client_id()
        context.facade.plan_service.audit.append(
            event_type="mcp_authorization_rejected",
            actor=f"agent:{client_id}",
            subject_id=subject_id,
            authority=authority,
            payload={
                "operation": operation,
                "client_principal_id": client_id,
                "error_code": error.code.value,
            },
        )
        raise
    policy = _authority_policy(context)
    if policy is not None:
        policy_authority = authority or policy.local_context()
        policy.authorize(policy_authority, operation=operation)
    _audit_client_request(context, operation=operation, subject_id=subject_id)


def _authorize_read(
    context: DomoticsMcpContext,
    *,
    operation: str,
    area_id: str | None = None,
    device_ids: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
) -> None:
    policy = _authority_policy(context)
    if policy is None:
        return
    authority = current_authority() or policy.local_context()
    if authority.area_ids and device_ids:
        known_areas = {device.id: device.area_id for device in context.registry.devices}
        if any(known_areas.get(device_id) not in authority.area_ids for device_id in device_ids):
            policy.authorize(authority, operation=operation, area_id="")
    policy.authorize(
        authority,
        operation=operation,
        area_id=area_id,
        device_ids=device_ids,
        capabilities=capabilities,
    )


def _visible_devices(context: DomoticsMcpContext) -> list[Any]:
    selected = list(context.registry.devices)
    authority = current_authority()
    if authority is None:
        return selected
    if authority.device_ids:
        selected = [device for device in selected if device.id in authority.device_ids]
    if authority.area_ids:
        selected = [device for device in selected if device.area_id in authority.area_ids]
    return selected


def _bind_request_plan(context: DomoticsMcpContext, plan: Plan, *, operation: str) -> Plan:
    policy = _authority_policy(context)
    if policy is None:
        return plan
    authority = current_authority() or policy.local_context()
    policy.authorize(authority, operation=operation)
    return policy.bind_plan(plan, authority, operation=operation)


def _authorize_existing_plan(context: DomoticsMcpContext, plan: Plan, *, operation: str) -> None:
    policy = _authority_policy(context)
    if policy is None:
        return
    authority = current_authority() or policy.local_context()
    policy.authorize(
        authority,
        operation=operation,
        target_household_id=plan.authority.household_id,
        device_ids=tuple(command.device_id for command in plan.commands),
        capabilities=tuple(command.command for command in plan.commands),
    )
    if authority.area_ids:
        known_areas = {device.id: device.area_id for device in context.registry.devices}
        if any(
            known_areas.get(command.device_id) not in authority.area_ids
            for command in plan.commands
        ):
            policy.authorize(authority, operation=operation, area_id="")


def _audit_client_request(context: DomoticsMcpContext, *, operation: str, subject_id: str) -> None:
    client_id = current_client_id()
    context.facade.plan_service.audit.append(
        event_type="mcp_request_authorized",
        actor=f"agent:{client_id}",
        subject_id=subject_id,
        authority=current_authority(),
        payload={
            "operation": operation,
            "client_principal_id": client_id,
        },
    )


async def _resolve_plan(context: DomoticsMcpContext, plan_id: str) -> Plan | None:
    plan = context.plans.get(plan_id)
    if plan is None and context.plan_repository is not None:
        plan = await context.plan_repository.get(plan_id)
        if plan is not None:
            context.plans[plan.id] = plan
    return plan


async def _admit_mcp_operation(
    context: DomoticsMcpContext, plan: Plan, operation: AdmissionOperation
) -> None:
    _authorize_existing_plan(context, plan, operation=operation.value)
    admission = context.facade.execution_admission
    if admission is None:
        bundle_persistence_configured = context.bundle_commit_service is not None or (
            context.scheduler is not None
            and getattr(context.scheduler, "bundle_repository", None) is not None
        )
        if bundle_persistence_configured:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Bundle persistence requires the authoritative execution admission boundary",
                details={"plan_id": plan.id},
            )
        return
    await admission.admit(plan, operation=operation)


async def _persist_plan(context: DomoticsMcpContext, plan: Plan) -> Plan:
    if context.plan_repository is not None:
        await context.plan_repository.save(plan)
    context.plans[plan.id] = plan
    return plan


async def _persist_validated_plan(context: DomoticsMcpContext, plan: Plan) -> Plan:
    if context.plan_repository is not None:
        await context.plan_repository.save_validation(plan)
    context.plans[plan.id] = plan
    return plan


def _command_validation_response(validated: Plan) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "plan_id": validated.id,
        "command": validated.commands[0].model_dump(mode="json"),
        "validation": validated.validation.model_dump(mode="json")
        if validated.validation
        else None,
        "policy_decision": validated.policy_decisions[0].model_dump(mode="json")
        if validated.policy_decisions
        else None,
    }


def _plan_validation_response(validated: Plan) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "plan": validated.model_dump(mode="json"),
        "validation": validated.validation.model_dump(mode="json")
        if validated.validation
        else None,
        "policy_decisions": [
            decision.model_dump(mode="json") for decision in validated.policy_decisions
        ],
    }


async def _validate_command(
    context: DomoticsMcpContext,
    command: Command | dict[str, Any],
    agent_request_id: str | None,
    *,
    persist: bool,
) -> dict[str, Any]:
    parsed_command = Command.model_validate(command)
    plan = Plan(
        id=f"command-validation-{parsed_command.id}",
        commands=[parsed_command],
        agent_request_id=agent_request_id or str(uuid4()),
    )
    plan = _bind_request_plan(
        context, plan, operation="preview" if not persist else "prepare_command"
    )
    validated = context.facade.validate_plan(plan)
    if persist:
        await _persist_validated_plan(context, validated)
    return _command_validation_response(validated)


async def _validate_plan(
    context: DomoticsMcpContext, plan: Plan | dict[str, Any], *, persist: bool
) -> dict[str, Any]:
    parsed_plan = Plan.model_validate(plan)
    if parsed_plan.agent_request_id is None:
        parsed_plan = parsed_plan.model_copy(update={"agent_request_id": str(uuid4())})
    parsed_plan = _bind_request_plan(
        context, parsed_plan, operation="preview" if not persist else "prepare_plan"
    )
    validated = context.facade.validate_plan(parsed_plan)
    if persist:
        await _persist_validated_plan(context, validated)
    return _plan_validation_response(validated)


async def _persist_approved_plan(context: DomoticsMcpContext, plan: Plan) -> Plan:
    if context.plan_repository is not None:
        await context.plan_repository.save_approval(plan)
    context.plans[plan.id] = plan
    return plan


def register_domotics_tools(server: FastMCP, context: DomoticsMcpContext) -> FastMCP:
    ensure_fastmcp_settings_ready()
    read_annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    mutation_annotations = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

    @server.tool(
        name="discover_devices",
        description="Read or refresh the canonical semantic device inventory.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def discover_devices(
        refresh: bool = False,
        area_id: str | None = None,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            _authorize_read(context, operation="discover_devices", area_id=area_id)
            if refresh:
                await context.discovery.refresh()
                context.last_refreshed_at = context.clock.now()
            selected = context.registry.devices
            if area_id is not None:
                selected = [device for device in selected if device.area_id == area_id]
            selected = [device for device in selected if device in _visible_devices(context)]
            if types is not None:
                requested_types = {DeviceType(device_type) for device_type in types}
                selected = [device for device in selected if device.type in requested_types]
            return inventory_snapshot(
                context.registry,
                runtime_revision=context.discovery.state_store.runtime_revision,
                refreshed_at=context.last_refreshed_at,
                devices=selected,
            )
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    commissioning_service = context.commissioning_service
    if commissioning_service is not None:

        @server.tool(
            name="inspect_commissioning",
            description=(
                "Read the shared, non-authoritative commissioning report for "
                "future battery and EV bindings."
            ),
            annotations=read_annotations,
            structured_output=True,
        )
        @with_request_principal
        async def inspect_commissioning(
            refresh: bool = False,
            asset_types: list[str] | None = None,
        ) -> dict[str, Any]:
            try:
                _authorize_read(context, operation="inspect")
                if refresh:
                    await context.discovery.refresh()
                    context.last_refreshed_at = context.clock.now()
                report = commissioning_service.inspect(
                    runtime_revision=context.discovery.state_store.runtime_revision,
                    asset_types=asset_types,
                    authority=current_authority(),
                )
                context.commissioning_report = report
                return report.model_dump(mode="json")
            except (DomainError, ValueError, ValidationError) as error:
                return error_envelope(error)

        @server.tool(
            name="verify_commissioning",
            description=(
                "Verify bounded commissioning evidence without creating authority "
                "or calling an adapter."
            ),
            annotations=read_annotations,
            structured_output=True,
        )
        @with_request_principal
        async def verify_commissioning(evidence: dict[str, Any]) -> dict[str, Any]:
            try:
                _authorize_read(context, operation="inspect")
                report = context.commissioning_report
                if report is None:
                    report = commissioning_service.inspect(
                        runtime_revision=context.discovery.state_store.runtime_revision,
                        authority=current_authority(),
                    )
                    context.commissioning_report = report
                parsed = CommissioningEvidence.model_validate(evidence)
                qualification = commissioning_service.verify_evidence(report, parsed)
                if context.qualification_repository is not None:
                    await context.qualification_repository.save(parsed.evidence_id, qualification)
                return qualification.model_dump(mode="json")
            except (DomainError, ValueError, ValidationError) as error:
                return error_envelope(error)

    privacy_service = context.privacy_service
    privacy_policy = context.privacy_policy
    if privacy_service is not None and privacy_policy is not None:

        @server.tool(
            name="export_household_data",
            description=(
                "Export policy-allowed local household data with credential-shaped fields "
                "redacted. Use next_cursor to retrieve bounded pages when "
                "total_record_count exceeds the page."
            ),
            annotations=read_annotations,
            structured_output=True,
        )
        @with_request_principal
        async def export_household_data(
            categories: list[str], cursor: str | None = None, limit: int | None = None
        ) -> dict[str, Any]:
            try:
                _authorize_read(context, operation="privacy_export")
                requester = current_authority() or privacy_policy.authority
                result = await privacy_service.export(
                    privacy_policy,
                    requester,
                    categories=categories,
                    cursor=cursor,
                    limit=limit,
                )
                return result.model_dump(mode="json")
            except (DomainError, PermissionError, ValueError, ValidationError) as error:
                return error_envelope(error)

        @server.tool(
            name="delete_household_data",
            description=(
                "Delete policy-allowed household data while preserving immutable "
                "security and audit evidence."
            ),
            annotations=mutation_annotations,
            structured_output=True,
        )
        @with_request_principal
        async def delete_household_data(categories: list[str], request_id: str) -> dict[str, Any]:
            try:
                _authorize_mutation(context, operation="privacy_delete", subject_id=request_id)
                requester = current_authority() or privacy_policy.authority
                result = await privacy_service.delete(
                    privacy_policy,
                    requester,
                    categories=categories,
                    request_id=request_id,
                )
                return result.model_dump(mode="json")
            except (DomainError, PermissionError, ValueError, ValidationError) as error:
                return error_envelope(error)

    @server.tool(
        name="get_state",
        description="Read bounded semantic state snapshots for selected devices.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def get_state(
        devices: list[str],
        capabilities: list[str] | None = None,
        allow_stale: bool = True,
    ) -> dict[str, Any]:
        try:
            _authorize_read(
                context,
                operation="get_state",
                device_ids=tuple(devices),
                capabilities=tuple(capabilities or ()),
            )
            read_result = await context.state_service.get_with_diagnostics(
                devices,
                capabilities,
                allow_stale=allow_stale,
            )
            return {
                "schema_version": "v1",
                "runtime_revision": context.discovery.state_store.runtime_revision,
                "states": [state.model_dump(mode="json") for state in read_result.states],
                "diagnostics": [item.as_dict() for item in read_result.diagnostics],
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="get_energy_context",
        description="Read a complete canonical energy context for one requested horizon.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def get_energy_context(horizon: dict[str, Any]) -> dict[str, Any]:
        try:
            _authorize_read(context, operation="get_energy_context")
            if context.energy_context_provider is None:
                raise ValueError("Energy context provider is unavailable")
            requested_horizon = Horizon.model_validate(horizon)
            if context.blocking_worker is None:
                # Fallback for a context built without a pre-wired worker
                # (e.g. ad-hoc test contexts). The production path
                # (mcp/stdio.py build_configured_server) always pre-supplies
                # one registered with RuntimeComposition.close(), so this
                # branch's worker is intentionally unowned/best-effort here.
                context.blocking_worker = OptimizationWorker(context.energy_context_provider)
            worker = context.blocking_worker
            energy_context = await worker.run_blocking(
                context.energy_context_provider.get_context,
                requested_horizon,
                timeout=context.provider_timeout_seconds,
            )
            parsed_context = EnergyContext.model_validate(energy_context)
            if parsed_context.horizon != requested_horizon:
                raise ValueError("Energy context horizon does not match the request")
            return energy_context_snapshot(
                parsed_context,
                context.discovery.state_store.runtime_revision,
            )
        except WorkerOperationError as error:
            if isinstance(error.cause, EnergyProviderError):
                return error_envelope(error.cause)
            return error_envelope(
                DomainError(
                    ErrorCode.VALIDATION_ERROR,
                    "Energy provider worker could not complete the request",
                    details={"worker_code": error.code},
                )
            )
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="validate_command",
        description=(
            "Legacy persistent alias for prepare_command; use preview_command for a read-only call."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def validate_command(
        command: Command, agent_request_id: str | None = None
    ) -> dict[str, Any]:
        try:
            parsed_command = Command.model_validate(command)
            _authorize_mutation(context, operation="validate_command", subject_id=parsed_command.id)
            return await _validate_command(context, command, agent_request_id, persist=True)
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="validate_plan",
        description=(
            "Legacy persistent alias for prepare_plan; use preview_plan for a read-only call."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def validate_plan(plan: Plan, mode: str = "prepare") -> dict[str, Any]:
        del mode
        try:
            parsed_plan = Plan.model_validate(plan)
            _authorize_mutation(context, operation="validate_plan", subject_id=parsed_plan.id)
            return await _validate_plan(context, plan, persist=True)
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="request_approval",
        description=(
            "Issue a server-authoritative approval grant for a plan requiring "
            "confirmation. A trusted host may inject an authenticated operator "
            "principal. The optional operator_token exists only for explicitly "
            "enabled local/dev compatibility mode; the caller never supplies "
            "the recorded operator identity. The "
            "returned approval_id is single-use and bound to the plan's "
            "current validation digest."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def request_approval(
        plan_id: str,
        validation_digest: str,
        operator_token: str | None = None,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
    ) -> dict[str, Any]:
        try:
            _authorize_mutation(context, operation="request_approval", subject_id=plan_id)
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            _authorize_existing_plan(context, plan, operation="request_approval")
            if plan.validation is None or plan.validation.digest != validation_digest:
                raise ValueError("Validation digest does not match the stored plan")
            principal = (
                context.operator_principal_provider()
                if context.operator_principal_provider is not None
                else None
            )
            if context.operator_approval_assertion_provider is not None:
                assertion = context.operator_approval_assertion_provider(
                    plan.id, validation_digest, bundle_digest
                )
                if assertion is None:
                    raise DomainError(
                        ErrorCode.APPROVAL_ASSERTION_REQUIRED,
                        "The trusted host did not provide an explicit human approval assertion",
                    )
                if principal is None:
                    principal = assertion.principal
                grant = context.approval_store.issue_authenticated(
                    plan,
                    principal=principal,
                    assertion=assertion,
                    bundle_digest=bundle_digest,
                    recurrence_digest=recurrence_digest,
                )
            elif principal is not None:
                raise DomainError(
                    ErrorCode.APPROVAL_ASSERTION_REQUIRED,
                    "An authenticated operator principal is not human consent",
                )
            else:
                grant = context.approval_store.issue_legacy(
                    plan,
                    operator_token=operator_token,
                    bundle_digest=bundle_digest,
                    recurrence_digest=recurrence_digest,
                )
            return {
                "schema_version": "v1",
                "approval_id": grant.approval_id,
                "plan_id": grant.plan_id,
                "validation_digest": grant.validation_digest,
                "bundle_digest": grant.bundle_digest,
                "recurrence_digest": grant.recurrence_digest,
                "validation_valid_until": (
                    grant.validation_valid_until.isoformat()
                    if grant.validation_valid_until is not None
                    else None
                ),
                "issued_at": grant.issued_at.isoformat(),
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="execute_plan",
        description="Execute a previously validated plan after runtime safety checks.",
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def execute_plan(
        plan_id: str,
        validation_digest: str,
        approval_id: str | None = None,
        bundle_digest: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        try:
            _authorize_mutation(context, operation="execute_plan", subject_id=plan_id)
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            await _admit_mcp_operation(context, plan, AdmissionOperation.EXECUTE)
            if plan.validation is None or plan.validation.digest != validation_digest:
                raise ValueError("Validation digest does not match the stored plan")
            if (
                context.bundle_commit_service is not None
                and await context.bundle_commit_service.is_member(plan_id)
            ):
                raise DomainError(
                    ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN,
                    "Bundle members require execution through the bundle aggregate",
                )
            if plan.status is PlanStatus.REQUIRES_CONFIRMATION:
                if approval_id is None:
                    raise ValueError("Plan requires an approval_id issued via request_approval")
                grant = context.approval_store.validate(
                    approval_id, plan, bundle_digest=bundle_digest
                )
                if not dry_run:
                    context.approval_store.consume(
                        approval_id, plan, bundle_digest=bundle_digest
                    )
                    plan = context.facade.approve_plan(plan, grant=grant)
                    await _persist_approved_plan(context, plan)
            if dry_run:
                return {
                    "schema_version": "v1",
                    "dry_run": True,
                    "plan": plan.model_dump(mode="json"),
                }
            execution = await context.facade.execute_plan(plan)
            if context.plan_repository is not None:
                persisted = await context.plan_repository.get(plan.id)
                if persisted is not None:
                    context.plans[persisted.id] = persisted
            return {
                "schema_version": "v1",
                "plan_id": plan.id,
                "outcomes": [outcome.model_dump(mode="json") for outcome in execution.outcomes],
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="schedule_plan",
        description=(
            "Schedule a previously validated/approved plan to execute at a "
            "future time, instead of immediately. The plan still goes "
            "through every existing safety check when its time arrives."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def schedule_plan(
        plan_id: str,
        validation_digest: str,
        execute_at: str,
        approval_id: str | None = None,
        bundle_digest: str | None = None,
    ) -> dict[str, Any]:
        try:
            _authorize_mutation(context, operation="schedule_plan", subject_id=plan_id)
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            await _admit_mcp_operation(context, plan, AdmissionOperation.SCHEDULE)
            if plan.validation is None or plan.validation.digest != validation_digest:
                raise ValueError("Validation digest does not match the stored plan")
            if (
                context.bundle_commit_service is not None
                and await context.bundle_commit_service.is_member(plan_id)
            ):
                raise DomainError(
                    ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN,
                    "Bundle members require scheduling through the bundle aggregate",
                )
            parsed_execute_at = _parse_timezone_aware_datetime(execute_at)
            if plan.execute_at != parsed_execute_at or plan.execution_window is None:
                raise DomainError(
                    ErrorCode.SCHEDULE_EVIDENCE_MISMATCH,
                    "The plan must be validated with its exact execution window before scheduling",
                )
            approval_reservation_id: str | None = None
            approval_reserved = False
            scheduled = False
            if plan.status is PlanStatus.REQUIRES_CONFIRMATION:
                if approval_id is None:
                    raise DomainError(
                        ErrorCode.APPROVAL_REQUIRED,
                        "Plan requires an approval_id issued via request_approval",
                    )
                approval_reservation_id = (
                    f"schedule:{plan.id}:{validation_digest}:{parsed_execute_at.isoformat()}"
                )
                grant = context.approval_store.reserve(
                    approval_id,
                    plan,
                    reservation_id=approval_reservation_id,
                    bundle_digest=bundle_digest,
                )
                approval_reserved = True
                plan = context.facade.approve_plan(plan, grant=grant)
            scheduled_expiry = parsed_execute_at + context.facade.plan_service.DEFAULT_PLAN_TTL
            scheduled_plan = plan.model_copy(
                update={
                    "execute_at": parsed_execute_at,
                    "expires_at": max(plan.expires_at or parsed_execute_at, scheduled_expiry),
                }
            )
            try:
                await context.scheduler.schedule(scheduled_plan)
                scheduled = True
                await _persist_plan(context, scheduled_plan)
                if approval_reserved and approval_reservation_id is not None:
                    context.approval_store.commit_reservation(approval_reservation_id)
            except Exception:
                if scheduled or approval_reserved:
                    await context.scheduler.cancel(plan.id)
                if approval_reserved and approval_reservation_id is not None:
                    context.approval_store.release_reservation(approval_reservation_id)
                raise
            return {
                "schema_version": "v1",
                "plan_id": scheduled_plan.id,
                "execute_at": scheduled_plan.execute_at.isoformat()
                if scheduled_plan.execute_at
                else None,
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    if context.bundle_commit_service is not None:

        @server.tool(
            name="commit_or_schedule_bundle",
            description=(
                "Commit one validated bundle through the runtime-owned physical "
                "execution and scheduling boundary."
            ),
            annotations=mutation_annotations,
            structured_output=True,
        )
        @with_request_principal
        async def commit_or_schedule_bundle(
            bundle_digest: str,
            scenario_id: str,
            members: list[dict[str, Any]],
        ) -> dict[str, Any]:
            try:
                _authorize_mutation(
                    context,
                    operation="commit_or_schedule_bundle",
                    subject_id=bundle_digest,
                )
                request = BundleCommitRequest.model_validate(
                    {
                        "bundle_digest": bundle_digest,
                        "scenario_id": scenario_id,
                        "members": members,
                    }
                )
                service = context.bundle_commit_service
                assert service is not None
                result = await service.commit(request)
                return {
                    "schema_version": "v1",
                    "bundle_commit_id": result.id,
                    "bundle_digest": result.bundle_digest,
                    "status": result.status.value,
                    "members": [
                        {
                            "plan_id": member.plan_id,
                            "status": member.status.value,
                            "execution_status": (
                                member.execution_status.value
                                if member.execution_status is not None
                                else None
                            ),
                            "error_code": member.error_code,
                        }
                        for member in result.members
                    ],
                }
            except (DomainError, ValueError, ValidationError) as error:
                return error_envelope(error)

        @server.tool(
            name="execute_scene",
            description=(
                "Execute one named, ordered scene through the validated bundle "
                "boundary, including approval, admission and physical readback."
            ),
            annotations=mutation_annotations,
            structured_output=True,
        )
        @with_request_principal
        async def execute_scene(
            scene_id: str,
            scene_digest: str,
            scenario_id: str,
            runtime_revision: str,
            bundle_digest: str,
            members: list[dict[str, Any]],
        ) -> dict[str, Any]:
            try:
                request = SceneCommitRequest.model_validate(
                    {
                        "scene_id": scene_id,
                        "scene_digest": scene_digest,
                        "scenario_id": scenario_id,
                        "runtime_revision": runtime_revision,
                        "bundle_digest": bundle_digest,
                        "members": members,
                    }
                )
                _authorize_mutation(context, operation="execute_scene", subject_id=request.scene_id)
                expected_scene_digest = scene_commit_digest(
                    scene_id=request.scene_id,
                    scenario_id=request.scenario_id,
                    runtime_revision=request.runtime_revision,
                    bundle_digest=request.bundle_digest,
                    members=request.members,
                )
                if request.scene_digest != expected_scene_digest:
                    raise DomainError(
                        ErrorCode.VALIDATION_ERROR,
                        "Scene digest does not match its ordered members and runtime revision",
                    )
                current_revision = context.facade.plan_service.current_revision
                if request.runtime_revision != current_revision:
                    raise DomainError(
                        ErrorCode.STALE_PLAN,
                        "Scene runtime revision is stale; revalidate the scene",
                    )
                for member in request.members:
                    plan = await _resolve_plan(context, member.plan_id)
                    if plan is None:
                        raise DomainError(
                            ErrorCode.VALIDATION_ERROR,
                            f"Unknown scene plan: {member.plan_id}",
                        )
                    if (
                        plan.validation is None
                        or plan.validation.runtime_revision != request.runtime_revision
                    ):
                        raise DomainError(
                            ErrorCode.STALE_PLAN,
                            "Scene member validation is stale; revalidate the scene",
                            details={"plan_id": member.plan_id},
                        )
                    _authorize_existing_plan(context, plan, operation="execute_scene")
                service = context.bundle_commit_service
                assert service is not None
                result = await service.commit(
                    BundleCommitRequest(
                        bundle_digest=request.bundle_digest,
                        scenario_id=request.scenario_id,
                        members=request.members,
                    )
                )
                return {
                    "schema_version": "v1",
                    "scene_id": request.scene_id,
                    "scene_digest": request.scene_digest,
                    "bundle_commit_id": result.id,
                    "bundle_digest": result.bundle_digest,
                    "status": result.status.value,
                    "members": [
                        {
                            "plan_id": member.plan_id,
                            "status": member.status.value,
                            "execution_status": (
                                member.execution_status.value
                                if member.execution_status is not None
                                else None
                            ),
                            "error_code": member.error_code,
                        }
                        for member in result.members
                    ],
                }
            except (DomainError, ValueError, ValidationError) as error:
                return error_envelope(error)

    @server.tool(
        name="cancel_scheduled_plan",
        description="Cancel a pending scheduled plan before it executes.",
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def cancel_scheduled_plan(plan_id: str) -> dict[str, Any]:
        try:
            _authorize_mutation(context, operation="cancel_scheduled_plan", subject_id=plan_id)
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            if (
                context.bundle_commit_service is not None
                and await context.bundle_commit_service.is_member(plan_id)
            ):
                raise DomainError(
                    ErrorCode.BUNDLE_MEMBER_CANCEL_FORBIDDEN,
                    "Bundle members require cancellation through the bundle aggregate",
                )
            cancelled = await context.scheduler.cancel(plan_id)
            if cancelled:
                cancelled_plan = context.facade.plan_service.cancel(plan)
                await _persist_plan(context, cancelled_plan)
            return {"schema_version": "v1", "plan_id": plan_id, "cancelled": cancelled}
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="reschedule_plan",
        description=(
            "Request a temporal revision for a pending plan. The legacy generic "
            "mutation is fail-closed; a changed time must be validated and "
            "approved again before admission."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def reschedule_plan(
        plan_id: str,
        execute_at: str,
        validation_digest: str | None = None,
        schedule_revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            _authorize_mutation(context, operation="reschedule_plan", subject_id=plan_id)
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            await _admit_mcp_operation(context, plan, AdmissionOperation.RESCHEDULE)
            parsed_execute_at = _parse_timezone_aware_datetime(execute_at)
            scheduled = await context.scheduler.repository.get(plan_id)
            if scheduled is None or scheduled[1] != "pending":
                return {"schema_version": "v1", "plan_id": plan_id, "rescheduled": False}
            scheduled_plan, _status = scheduled
            context.facade.plan_service.audit.append(
                event_type="reschedule_rejected",
                actor="runtime",
                subject_id=plan_id,
                payload={
                    "reason": ErrorCode.RESCHEDULE_REQUIRES_REVALIDATION.value,
                    "old_execute_at": scheduled_plan.execute_at.isoformat()
                    if scheduled_plan.execute_at
                    else None,
                    "requested_execute_at": parsed_execute_at.isoformat(),
                    "old_validation_digest": scheduled_plan.validation.digest
                    if scheduled_plan.validation
                    else None,
                    "old_schedule_revision": scheduled_plan.schedule_revision,
                },
            )
            raise DomainError(
                ErrorCode.RESCHEDULE_REQUIRES_REVALIDATION,
                "Rescheduling changes physical intent; create and validate a new temporal revision",
                details={
                    "old_execute_at": scheduled_plan.execute_at.isoformat()
                    if scheduled_plan.execute_at
                    else None,
                    "requested_execute_at": parsed_execute_at.isoformat(),
                    "old_validation_digest": scheduled_plan.validation.digest
                    if scheduled_plan.validation
                    else None,
                    "old_schedule_revision": scheduled_plan.schedule_revision,
                    "provided_validation_digest": validation_digest,
                    "provided_schedule_revision": schedule_revision,
                    "requires_revalidation": True,
                },
            )
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="list_scheduled_plans",
        description="List plans currently pending their scheduled execution time.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def list_scheduled_plans() -> dict[str, Any]:
        try:
            _authorize_read(context, operation="list")
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            pending = await context.scheduler.list_pending()
            visible: list[Plan] = []
            for plan in pending:
                try:
                    _authorize_existing_plan(context, plan, operation="list")
                except DomainError:
                    continue
                visible.append(plan)
            return {
                "schema_version": "v1",
                "plans": [
                    {
                        "plan_id": plan.id,
                        "execute_at": plan.execute_at.isoformat() if plan.execute_at else None,
                    }
                    for plan in visible
                ],
            }
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="schedule_recurring_plan",
        description=(
            "Schedule a plan's commands to run repeatedly at a fixed local "
            "time, optionally restricted to specific weekdays. Creating a "
            "standing automation is its own authority act, distinct from "
            "running the commands once: if the template plan currently "
            "requires confirmation, an approval_id from request_approval is "
            "required to create the schedule at all. Every occurrence is "
            "still independently revalidated against live state before it "
            "executes; an occurrence requiring confirmation at run time is "
            "skipped and audited, never auto-approved, and recurrence "
            "continues to its next scheduled time. An optional expires_at "
            "bounds how long the automation stays active."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def schedule_recurring_plan(
        plan_id: str,
        time_of_day: str,
        timezone: str,
        days_of_week: list[int] | None = None,
        approval_id: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        try:
            _authorize_mutation(context, operation="schedule_recurring_plan", subject_id=plan_id)
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            hour, minute = (int(part) for part in time_of_day.split(":"))
            rule = RecurrenceRule(
                authority=plan.authority,
                time_of_day=time(hour=hour, minute=minute),
                timezone=timezone,
                days_of_week=days_of_week,
                expires_at=(
                    _parse_timezone_aware_datetime(expires_at) if expires_at is not None else None
                ),
            )
            expected_recurrence_digest = recurrence_digest(plan.id, rule)
            grant = None
            if plan.status in {
                PlanStatus.REQUIRES_CONFIRMATION,
                PlanStatus.APPROVED,
                PlanStatus.READY,
            }:
                if approval_id is None:
                    raise DomainError(
                        ErrorCode.APPROVAL_REQUIRED,
                        "Creating a standing automation needs a recurrence-scoped "
                        "approval_id from request_approval",
                    )
                grant = context.approval_store.consume(
                    approval_id,
                    plan,
                    recurrence_digest=expected_recurrence_digest,
                )
            schedule_id = f"recurring:{plan_id}:{context.clock.now().isoformat()}"
            first_occurrence = await context.scheduler.schedule_recurring(
                schedule_id, plan.commands, rule, plan=plan, approval=grant
            )
            await _admit_mcp_operation(context, plan, AdmissionOperation.STANDING_AUTOMATION)
            schedule_id = f"recurring:{plan_id}:{expected_recurrence_digest}"
            existing = await context.scheduler.recurring_repository.get(schedule_id)  # type: ignore[union-attr]
            if existing is not None and existing[3] == "active":
                existing_authority = await context.scheduler.recurring_repository.get_authority(  # type: ignore[union-attr]
                    schedule_id
                )
                if existing_authority is not None:
                    return {
                        "schema_version": "v1",
                        "schedule_id": schedule_id,
                        "next_execute_at": existing[2].isoformat(),
                    }
            grant = None
            if plan.status in {
                PlanStatus.REQUIRES_CONFIRMATION,
                PlanStatus.APPROVED,
                PlanStatus.READY,
            }:
                if approval_id is None:
                    raise DomainError(
                        ErrorCode.APPROVAL_REQUIRED,
                        "Creating a standing automation needs a recurrence-scoped "
                        "approval_id from request_approval",
                    )
                grant = context.approval_store.reserve(
                    approval_id,
                    plan,
                    reservation_id=schedule_id,
                    recurrence_digest=expected_recurrence_digest,
                )
            authority = {
                "schema": "standing-automation-authority-v1",
                "plan_id": plan.id,
                "template_digest": template_digest,
                "recurrence_digest": expected_recurrence_digest,
                "validation_digest": plan.validation.digest if plan.validation else None,
                "policy_decisions": [
                    decision.model_dump(mode="json") for decision in plan.policy_decisions
                ],
                "owner": grant.approved_by if grant is not None else "runtime",
                "approval_id": grant.approval_id if grant is not None else None,
                "expires_at": (
                    rule.expires_at.isoformat() if rule.expires_at is not None else None
                ),
            }
            try:
                first_occurrence = await context.scheduler.schedule_recurring(
                    schedule_id,
                    plan.commands,
                    rule,
                    plan=plan,
                    approval=grant,
                    authority=authority,
                )
                if grant is not None:
                    context.approval_store.commit_reservation(schedule_id)
            except Exception:
                if grant is not None:
                    context.approval_store.release_reservation(schedule_id)
                await context.scheduler.cancel_recurring(schedule_id)
                raise
            return {
                "schema_version": "v1",
                "schedule_id": schedule_id,
                "next_execute_at": first_occurrence.isoformat(),
            }
        except (ValueError, ValidationError, DomainError) as error:
            return error_envelope(error)

    @server.tool(
        name="cancel_recurring_schedule",
        description="Cancel a recurring schedule; stops all future occurrences.",
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def cancel_recurring_schedule(schedule_id: str) -> dict[str, Any]:
        try:
            _authorize_mutation(
                context, operation="cancel_recurring_schedule", subject_id=schedule_id
            )
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            cancelled = await context.scheduler.cancel_recurring(schedule_id)
            return {"schema_version": "v1", "schedule_id": schedule_id, "cancelled": cancelled}
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="list_recurring_schedules",
        description="List active recurring schedules and their next occurrence.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def list_recurring_schedules() -> dict[str, Any]:
        try:
            _authorize_read(context, operation="list")
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            active = await context.scheduler.list_recurring()
            visible = []
            for schedule_id, commands, _rule, next_execute_at in active:
                try:
                    _authorize_read(
                        context,
                        operation="list",
                        device_ids=tuple(command.device_id for command in commands),
                    )
                except DomainError:
                    continue
                visible.append((schedule_id, next_execute_at))
            return {
                "schema_version": "v1",
                "schedules": [
                    {
                        "schedule_id": schedule_id,
                        "next_execute_at": next_execute_at.isoformat(),
                    }
                    for schedule_id, next_execute_at in visible
                ],
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="create_local_automation_rule",
        description=(
            "Create an offline local rule from a typed trigger and bounded plan. "
            "The supplied approval_id must be a server-issued standing approval "
            "whose recurrence_digest equals the rule definition digest."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def create_local_automation_rule(
        rule: dict[str, Any], approval_id: str
    ) -> dict[str, Any]:
        try:
            parsed = AutomationRule.model_validate(rule)
            _authorize_mutation(
                context, operation="create_local_automation_rule", subject_id=parsed.id
            )
            if context.local_automation is None:
                raise ValueError("Local automation is unavailable in this deployment")
            expires_at = parsed.expires_at
            if expires_at is None or expires_at <= context.clock.now():
                raise ValueError("persistent local automation requires a future expires_at")
            bound_template = _bind_request_plan(
                context, parsed.plan_template, operation="create_local_automation_rule"
            )
            parsed = parsed.model_copy(
                update={"authority": bound_template.authority, "plan_template": bound_template}
            )
            validated_template = context.facade.validate_plan(parsed.plan_template)
            rule_digest = automation_rule_digest(parsed)
            grant = context.approval_store.reserve(
                approval_id,
                validated_template,
                reservation_id=parsed.id,
                recurrence_digest=rule_digest,
            )
            consent = AutomationConsent(
                approval_id=grant.approval_id,
                principal_id=grant.approved_by,
                scope=parsed.scope,
                rule_digest=rule_digest,
                approved_at=grant.approved_at or grant.issued_at,
                expires_at=expires_at,
                authority=grant.authority,
            )
            enabled = parsed.model_copy(update={"status": AutomationRuleStatus.ENABLED})
            try:
                await context.local_automation.register(enabled, consent)
                context.approval_store.commit_reservation(parsed.id)
            except Exception:
                context.approval_store.release_reservation(parsed.id)
                raise
            return {
                "schema_version": "v1",
                "rule_id": enabled.id,
                "definition_digest": enabled.definition_digest,
                "status": enabled.status.value,
                "scope": enabled.scope,
                "expires_at": (
                    consent.expires_at.isoformat() if enabled.expires_at is not None else None
                ),
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="update_local_automation_rule",
        description=(
            "Replace one offline local rule version. A new server-issued standing "
            "approval matching the new definition digest is mandatory; the previous "
            "approval is invalidated before the updated rule can fire."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def update_local_automation_rule(
        rule: dict[str, Any], approval_id: str
    ) -> dict[str, Any]:
        reservation_id: str | None = None
        try:
            parsed = AutomationRule.model_validate(rule)
            _authorize_mutation(
                context, operation="update_local_automation_rule", subject_id=parsed.id
            )
            if context.local_automation is None:
                raise ValueError("Local automation is unavailable in this deployment")
            expires_at = parsed.expires_at
            if expires_at is None or expires_at <= context.clock.now():
                raise ValueError("persistent local automation requires a future expires_at")
            bound_template = _bind_request_plan(
                context, parsed.plan_template, operation="update_local_automation_rule"
            )
            parsed = parsed.model_copy(
                update={"authority": bound_template.authority, "plan_template": bound_template}
            )
            validated_template = context.facade.validate_plan(parsed.plan_template)
            rule_digest = automation_rule_digest(parsed)
            reservation_id = f"{parsed.id}:update:{uuid4().hex}"
            grant = context.approval_store.reserve(
                approval_id,
                validated_template,
                reservation_id=reservation_id,
                recurrence_digest=rule_digest,
            )
            consent = AutomationConsent(
                approval_id=grant.approval_id,
                principal_id=grant.approved_by,
                scope=parsed.scope,
                rule_digest=rule_digest,
                approved_at=grant.approved_at or grant.issued_at,
                expires_at=expires_at,
                authority=grant.authority,
            )
            enabled = parsed.model_copy(update={"status": AutomationRuleStatus.ENABLED})
            try:
                await context.local_automation.update(enabled, consent)
                context.approval_store.commit_reservation(reservation_id)
            except Exception:
                context.approval_store.release_reservation(reservation_id)
                raise
            return {
                "schema_version": "v1",
                "rule_id": enabled.id,
                "definition_digest": enabled.definition_digest,
                "status": enabled.status.value,
                "scope": enabled.scope,
                "expires_at": consent.expires_at.isoformat(),
            }
        except (DomainError, ValueError, ValidationError) as error:
            if reservation_id is not None:
                context.approval_store.release_reservation(reservation_id)
            return error_envelope(error)

    @server.tool(
        name="list_local_automation_rules",
        description="List typed local rules and their bounded lifecycle state.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def list_local_automation_rules() -> dict[str, Any]:
        try:
            _authorize_read(context, operation="list")
            if context.local_automation is None:
                raise ValueError("Local automation is unavailable in this deployment")
            records = await context.local_automation.list_rules()
            visible = []
            for record in records:
                try:
                    _authorize_existing_plan(context, record.rule.plan_template, operation="list")
                except DomainError:
                    continue
                visible.append(record)
            return {
                "schema_version": "v1",
                "rules": [
                    {
                        "rule_id": record.rule.id,
                        "name": record.rule.name,
                        "status": record.rule.status.value,
                        "definition_digest": record.rule.definition_digest,
                        "scope": record.rule.scope,
                        "trigger": record.rule.trigger.model_dump(mode="json"),
                        "cooldown_seconds": record.rule.cooldown_seconds,
                        "expires_at": (
                            record.rule.expires_at.isoformat()
                            if record.rule.expires_at is not None
                            else None
                        ),
                        "last_event_id": record.last_event_id,
                        "last_fired_at": (
                            record.last_fired_at.isoformat()
                            if record.last_fired_at is not None
                            else None
                        ),
                    }
                    for record in visible
                ],
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="set_local_automation_status",
        description="Enable or disable one local rule; enabling rechecks standing consent.",
        annotations=mutation_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def set_local_automation_status(rule_id: str, status: str) -> dict[str, Any]:
        try:
            _authorize_mutation(
                context, operation="set_local_automation_status", subject_id=rule_id
            )
            if context.local_automation is None:
                raise ValueError("Local automation is unavailable in this deployment")
            requested = AutomationRuleStatus(status)
            changed = await context.local_automation.set_status(rule_id, requested)
            return {
                "schema_version": "v1",
                "rule_id": rule_id,
                "status": requested.value,
                "changed": changed,
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="list_audit_events",
        description="Query the bounded, filterable audit trail of runtime decisions.",
        annotations=read_annotations,
        structured_output=True,
    )
    @with_request_principal
    async def list_audit_events(
        event_type: str | None = None,
        subject_id: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            _authorize_read(context, operation="list")
            if context.audit_repository is None:
                raise ValueError("The audit trail is not available in this deployment")
            events = await context.audit_repository.list_events(
                event_type=event_type,
                subject_id=subject_id,
                since=(_parse_timezone_aware_datetime(since) if since is not None else None),
                limit=limit,
            )
            authority = current_authority()
            if authority is not None:
                events = [
                    event
                    for event in events
                    if event.authority.tenant_id == authority.tenant_id
                    and event.authority.household_id in authority.household_ids
                ]
            return {
                "schema_version": "v1",
                "events": [event.model_dump(mode="json") for event in events],
            }
        except (DomainError, ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.resource("domotics://areas", mime_type="application/json")
    async def areas_resource() -> str:
        try:
            _authorize_read(context, operation="discover_devices")
            authority = current_authority()
            areas = context.registry.areas
            if authority is not None and authority.area_ids:
                areas = [area for area in areas if area.id in authority.area_ids]
            return as_json(
                {
                    "schema_version": "v1",
                    "runtime_revision": context.discovery.state_store.runtime_revision,
                    "areas": [area.model_dump(mode="json") for area in areas],
                }
            )
        except DomainError as error:
            return as_json(error_envelope(error))

    @server.resource("domotics://capabilities", mime_type="application/json")
    async def capabilities_resource() -> str:
        try:
            _authorize_read(context, operation="inspect")
            return as_json(
                capabilities_snapshot(
                    context.registry,
                    context.discovery.state_store.runtime_revision,
                    _visible_devices(context),
                )
            )
        except DomainError as error:
            return as_json(error_envelope(error))

    @server.resource("domotics://coverage", mime_type="application/json")
    async def coverage_resource() -> str:
        try:
            _authorize_read(context, operation="inspect")
            return as_json(
                coverage_snapshot(
                    context.registry,
                    runtime_revision=context.discovery.state_store.runtime_revision,
                    active_provider_ids=context.active_provider_ids,
                    devices=_visible_devices(context),
                )
            )
        except DomainError as error:
            return as_json(error_envelope(error))

    @server.resource("domotics://devices", mime_type="application/json")
    async def devices_resource() -> str:
        try:
            _authorize_read(context, operation="discover_devices")
            return as_json(
                inventory_snapshot(
                    context.registry,
                    runtime_revision=context.discovery.state_store.runtime_revision,
                    refreshed_at=context.last_refreshed_at,
                    devices=_visible_devices(context),
                )
            )
        except DomainError as error:
            return as_json(error_envelope(error))

    @server.resource("domotics://energy", mime_type="application/json")
    async def energy_resource() -> str:
        try:
            _authorize_read(context, operation="get_energy_context")
            return as_json(
                energy_snapshot(
                    context.registry,
                    context.discovery.state_store.runtime_revision,
                    _visible_devices(context),
                )
            )
        except DomainError as error:
            return as_json(error_envelope(error))

    @server.resource("domotics://policies", mime_type="application/json")
    async def policies_resource() -> str:
        try:
            _authorize_read(context, operation="inspect")
            return as_json(
                policies_snapshot(
                    context.policies,
                    context.discovery.state_store.runtime_revision,
                )
            )
        except DomainError as error:
            return as_json(error_envelope(error))

    @server.resource("domotics://metrics", mime_type="application/json")
    async def metrics_resource() -> str:
        try:
            _authorize_read(context, operation="inspect")
            if context.metrics is None:
                return as_json({"schema_version": "v1", "available": False})
            snapshot = await context.metrics.snapshot()
            return as_json({**snapshot, "available": True})
        except DomainError as error:
            return as_json(error_envelope(error))

    @server.resource("domotics://runtime", mime_type="application/json")
    async def runtime_resource() -> str:
        try:
            _authorize_read(context, operation="inspect")
            return as_json(
                runtime_snapshot(
                    context.registry,
                    runtime_revision=context.discovery.state_store.runtime_revision,
                    active_provider_ids=context.active_provider_ids,
                    battery_qualification=context.battery_qualification,
                    devices=_visible_devices(context),
                )
            )
        except DomainError as error:
            return as_json(error_envelope(error))

    if commissioning_service is not None:

        @server.resource("domotics://commissioning", mime_type="application/json")
        async def commissioning_resource() -> str:
            try:
                _authorize_read(context, operation="inspect")
                report = context.commissioning_report
                if report is None:
                    report = commissioning_service.inspect(
                        runtime_revision=context.discovery.state_store.runtime_revision,
                        authority=current_authority(),
                    )
                    context.commissioning_report = report
                return as_json(report.model_dump(mode="json"))
            except DomainError as error:
                return as_json(error_envelope(error))

    @server.prompt(
        name="discover-domotics-inventory", description="Guide semantic inventory discovery."
    )
    def discover_domotics_inventory_prompt() -> str:
        return (
            "Use domotics://devices, domotics://capabilities and get_state. "
            "Do not execute commands."
        )

    @server.prompt(name="diagnose-domotics-device", description="Guide safe device diagnosis.")
    def diagnose_domotics_device_prompt() -> str:
        return (
            "Inspect domotics://coverage, state and history, then report evidence "
            "and unavailable routes without mutating state."
        )

    @server.prompt(name="prepare-energy-plan", description="Guide proposal-only energy planning.")
    def prepare_energy_plan_prompt() -> str:
        return (
            "Read energy context and policies, call proposal-only optimization, "
            "validate the plan, and request approval before mutation."
        )

    return server


def create_domotics_server(context: DomoticsMcpContext) -> FastMCP:
    """Create the legacy domain-only server used by focused contract tests."""

    ensure_fastmcp_settings_ready()
    return register_domotics_tools(
        FastMCP(
            "DomoAI Domotics",
            instructions=(
                "Semantic home automation tools. Runtime policy and approval are authoritative."
            ),
        ),
        context,
    )
