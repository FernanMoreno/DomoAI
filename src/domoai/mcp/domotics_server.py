"""Semantic MCP v1 server backed by the shared application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.optimization_worker import OptimizationWorker, WorkerOperationError
from domoai.application.state_service import StateService
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import Command, DeviceType, Plan, PlanStatus, Policy, RecurrenceRule
from domoai.mcp.compat import ensure_fastmcp_settings_ready
from domoai.mcp.errors import error_envelope
from domoai.mcp.resources import (
    as_json,
    capabilities_snapshot,
    energy_context_snapshot,
    energy_snapshot,
    inventory_snapshot,
    policies_snapshot,
)
from domoai.optimizer.energy import EnergyContext
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.ports import EnergyContextProvider
from domoai.optimizer.providers import EnergyProviderError
from domoai.persistence.repositories import AuditEventRepository, PlanRepository
from domoai.runtime.approval_store import (
    ApprovalStore,
    OperatorApprovalAssertionProvider,
    OperatorPrincipalProvider,
)
from domoai.runtime.bundle_commit import BundleCommitRequest, BundleCommitService
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.metrics import RuntimeMetricsCollector
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler


@dataclass
class DomoticsMcpContext:
    discovery: DiscoveryService
    state_service: StateService
    facade: DomoticsFacade
    registry: DeviceRegistry
    policies: list[Policy]
    plan_repository: PlanRepository | None = None
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


def _parse_timezone_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


async def _resolve_plan(context: DomoticsMcpContext, plan_id: str) -> Plan | None:
    plan = context.plans.get(plan_id)
    if plan is None and context.plan_repository is not None:
        plan = await context.plan_repository.get(plan_id)
        if plan is not None:
            context.plans[plan.id] = plan
    return plan


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
    async def discover_devices(
        refresh: bool = False,
        area_id: str | None = None,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            if refresh:
                await context.discovery.refresh()
                context.last_refreshed_at = context.clock.now()
            selected = context.registry.devices
            if area_id is not None:
                selected = [device for device in selected if device.area_id == area_id]
            if types is not None:
                requested_types = {DeviceType(device_type) for device_type in types}
                selected = [device for device in selected if device.type in requested_types]
            return inventory_snapshot(
                context.registry,
                runtime_revision=context.discovery.state_store.runtime_revision,
                refreshed_at=context.last_refreshed_at,
                devices=selected,
            )
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="get_state",
        description="Read bounded semantic state snapshots for selected devices.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def get_state(
        devices: list[str],
        capabilities: list[str] | None = None,
        allow_stale: bool = True,
    ) -> dict[str, Any]:
        try:
            states = await context.state_service.get(
                devices,
                capabilities,
                allow_stale=allow_stale,
            )
            return {
                "schema_version": "v1",
                "runtime_revision": context.discovery.state_store.runtime_revision,
                "states": [state.model_dump(mode="json") for state in states],
            }
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="get_energy_context",
        description="Read a complete canonical energy context for one requested horizon.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def get_energy_context(horizon: dict[str, Any]) -> dict[str, Any]:
        try:
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
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="validate_command",
        description="Validate one semantic command without invoking an adapter.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def validate_command(
        command: dict[str, Any], agent_request_id: str | None = None
    ) -> dict[str, Any]:
        try:
            parsed_command = Command.model_validate(command)
            plan = Plan(
                id=f"command-validation-{parsed_command.id}",
                commands=[parsed_command],
                agent_request_id=agent_request_id or str(uuid4()),
            )
            validated = context.facade.validate_plan(plan)
            await _persist_validated_plan(context, validated)
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
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="validate_plan",
        description="Validate a semantic plan and return its policy decisions and digest.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def validate_plan(plan: dict[str, Any], mode: str = "preview") -> dict[str, Any]:
        del mode
        try:
            parsed_plan = Plan.model_validate(plan)
            if parsed_plan.agent_request_id is None:
                parsed_plan = parsed_plan.model_copy(update={"agent_request_id": str(uuid4())})
            validated = context.facade.validate_plan(parsed_plan)
            await _persist_validated_plan(context, validated)
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
        except (ValueError, ValidationError) as error:
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
    async def request_approval(
        plan_id: str,
        validation_digest: str,
        operator_token: str | None = None,
        bundle_digest: str | None = None,
    ) -> dict[str, Any]:
        try:
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
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
                )
            elif principal is not None:
                raise DomainError(
                    ErrorCode.APPROVAL_ASSERTION_REQUIRED,
                    "An authenticated operator principal is not human consent",
                )
            else:
                grant = context.approval_store.issue_legacy(
                    plan, operator_token=operator_token, bundle_digest=bundle_digest
                )
            return {
                "schema_version": "v1",
                "approval_id": grant.approval_id,
                "plan_id": grant.plan_id,
                "validation_digest": grant.validation_digest,
                "bundle_digest": grant.bundle_digest,
                "issued_at": grant.issued_at.isoformat(),
            }
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="execute_plan",
        description="Execute a previously validated plan after runtime safety checks.",
        annotations=mutation_annotations,
        structured_output=True,
    )
    async def execute_plan(
        plan_id: str,
        validation_digest: str,
        approval_id: str | None = None,
        bundle_digest: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        try:
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            if plan.validation is None or plan.validation.digest != validation_digest:
                raise ValueError("Validation digest does not match the stored plan")
            if plan.status is PlanStatus.REQUIRES_CONFIRMATION:
                if approval_id is None:
                    raise ValueError("Plan requires an approval_id issued via request_approval")
                grant = context.approval_store.consume(
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
        except (ValueError, ValidationError) as error:
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
    async def schedule_plan(
        plan_id: str,
        validation_digest: str,
        execute_at: str,
        approval_id: str | None = None,
        bundle_digest: str | None = None,
    ) -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            if plan.validation is None or plan.validation.digest != validation_digest:
                raise ValueError("Validation digest does not match the stored plan")
            parsed_execute_at = _parse_timezone_aware_datetime(execute_at)
            if plan.execute_at != parsed_execute_at or plan.execution_window is None:
                raise DomainError(
                    ErrorCode.SCHEDULE_EVIDENCE_MISMATCH,
                    "The plan must be validated with its exact execution window before scheduling",
                )
            if plan.status is PlanStatus.REQUIRES_CONFIRMATION:
                if approval_id is None:
                    raise ValueError("Plan requires an approval_id issued via request_approval")
                grant = context.approval_store.consume(
                    approval_id, plan, bundle_digest=bundle_digest
                )
                plan = context.facade.approve_plan(plan, grant=grant)
            scheduled_expiry = parsed_execute_at + context.facade.plan_service.DEFAULT_PLAN_TTL
            scheduled_plan = plan.model_copy(
                update={
                    "execute_at": parsed_execute_at,
                    "expires_at": max(plan.expires_at or parsed_execute_at, scheduled_expiry),
                }
            )
            await context.scheduler.schedule(scheduled_plan)
            await _persist_plan(context, scheduled_plan)
            return {
                "schema_version": "v1",
                "plan_id": scheduled_plan.id,
                "execute_at": scheduled_plan.execute_at.isoformat()
                if scheduled_plan.execute_at
                else None,
            }
        except (ValueError, ValidationError) as error:
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
        async def commit_or_schedule_bundle(
            bundle_digest: str,
            scenario_id: str,
            members: list[dict[str, Any]],
        ) -> dict[str, Any]:
            try:
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
        name="cancel_scheduled_plan",
        description="Cancel a pending scheduled plan before it executes.",
        annotations=mutation_annotations,
        structured_output=True,
    )
    async def cancel_scheduled_plan(plan_id: str) -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            cancelled = await context.scheduler.cancel(plan_id)
            if cancelled:
                cancelled_plan = context.facade.plan_service.cancel(plan)
                await _persist_plan(context, cancelled_plan)
            return {"schema_version": "v1", "plan_id": plan_id, "cancelled": cancelled}
        except (ValueError, ValidationError) as error:
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
    async def reschedule_plan(
        plan_id: str,
        execute_at: str,
        validation_digest: str | None = None,
        schedule_revision: int | None = None,
    ) -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            parsed_execute_at = _parse_timezone_aware_datetime(execute_at)
            is_bundle_member = (
                context.bundle_commit_service is not None
                and await context.bundle_commit_service.is_scheduled_member(plan_id)
            )
            if is_bundle_member:
                context.facade.plan_service.audit.append(
                    event_type="reschedule_rejected",
                    actor="runtime",
                    subject_id=plan_id,
                    payload={
                        "reason": ErrorCode.BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN.value,
                        "requested_execute_at": parsed_execute_at.isoformat(),
                    },
                )
                raise DomainError(
                    ErrorCode.BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN,
                    "Bundle members require a new bundle revision; generic rescheduling "
                    "is forbidden",
                )
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
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="list_scheduled_plans",
        description="List plans currently pending their scheduled execution time.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def list_scheduled_plans() -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            pending = await context.scheduler.list_pending()
            return {
                "schema_version": "v1",
                "plans": [
                    {
                        "plan_id": plan.id,
                        "execute_at": plan.execute_at.isoformat() if plan.execute_at else None,
                    }
                    for plan in pending
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
    async def schedule_recurring_plan(
        plan_id: str,
        time_of_day: str,
        timezone: str,
        days_of_week: list[int] | None = None,
        approval_id: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = await _resolve_plan(context, plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            grant = None
            if plan.status is PlanStatus.REQUIRES_CONFIRMATION:
                if approval_id is None:
                    raise DomainError(
                        ErrorCode.APPROVAL_REQUIRED,
                        "Creating a recurring automation from a plan that requires "
                        "confirmation needs an approval_id from request_approval",
                    )
                grant = context.approval_store.consume(approval_id, plan)
            hour, minute = (int(part) for part in time_of_day.split(":"))
            rule = RecurrenceRule(
                time_of_day=time(hour=hour, minute=minute),
                timezone=timezone,
                days_of_week=days_of_week,
                expires_at=(
                    _parse_timezone_aware_datetime(expires_at) if expires_at is not None else None
                ),
            )
            schedule_id = f"recurring:{plan_id}:{context.clock.now().isoformat()}"
            first_occurrence = await context.scheduler.schedule_recurring(
                schedule_id, plan.commands, rule, plan=plan, approval=grant
            )
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
    async def cancel_recurring_schedule(schedule_id: str) -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            cancelled = await context.scheduler.cancel_recurring(schedule_id)
            return {"schema_version": "v1", "schedule_id": schedule_id, "cancelled": cancelled}
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="list_recurring_schedules",
        description="List active recurring schedules and their next occurrence.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def list_recurring_schedules() -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            active = await context.scheduler.list_recurring()
            return {
                "schema_version": "v1",
                "schedules": [
                    {
                        "schedule_id": schedule_id,
                        "next_execute_at": next_execute_at.isoformat(),
                    }
                    for schedule_id, _commands, _rule, next_execute_at in active
                ],
            }
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="list_audit_events",
        description="Query the bounded, filterable audit trail of runtime decisions.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def list_audit_events(
        event_type: str | None = None,
        subject_id: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            if context.audit_repository is None:
                raise ValueError("The audit trail is not available in this deployment")
            events = await context.audit_repository.list_events(
                event_type=event_type,
                subject_id=subject_id,
                since=(_parse_timezone_aware_datetime(since) if since is not None else None),
                limit=limit,
            )
            return {
                "schema_version": "v1",
                "events": [event.model_dump(mode="json") for event in events],
            }
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.resource("domotics://areas", mime_type="application/json")
    async def areas_resource() -> str:
        return as_json(
            {
                "schema_version": "v1",
                "runtime_revision": context.discovery.state_store.runtime_revision,
                "areas": [area.model_dump(mode="json") for area in context.registry.areas],
            }
        )

    @server.resource("domotics://capabilities", mime_type="application/json")
    async def capabilities_resource() -> str:
        return as_json(
            capabilities_snapshot(
                context.registry,
                context.discovery.state_store.runtime_revision,
            )
        )

    @server.resource("domotics://devices", mime_type="application/json")
    async def devices_resource() -> str:
        return as_json(
            inventory_snapshot(
                context.registry,
                runtime_revision=context.discovery.state_store.runtime_revision,
                refreshed_at=context.last_refreshed_at,
            )
        )

    @server.resource("domotics://energy", mime_type="application/json")
    async def energy_resource() -> str:
        return as_json(
            energy_snapshot(context.registry, context.discovery.state_store.runtime_revision)
        )

    @server.resource("domotics://policies", mime_type="application/json")
    async def policies_resource() -> str:
        return as_json(
            policies_snapshot(
                context.policies,
                context.discovery.state_store.runtime_revision,
            )
        )

    @server.resource("domotics://metrics", mime_type="application/json")
    async def metrics_resource() -> str:
        if context.metrics is None:
            return as_json({"schema_version": "v1", "available": False})
        snapshot = await context.metrics.snapshot()
        return as_json({**snapshot, "available": True})

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
