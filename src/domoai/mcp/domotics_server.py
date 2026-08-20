"""Semantic MCP v1 server backed by the shared application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.state_service import StateService
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
from domoai.persistence.repositories import AuditEventRepository
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler


@dataclass
class DomoticsMcpContext:
    discovery: DiscoveryService
    state_service: StateService
    facade: DomoticsFacade
    registry: DeviceRegistry
    policies: list[Policy]
    approval_store: ApprovalStore = field(default_factory=ApprovalStore)
    energy_context_provider: EnergyContextProvider | None = None
    plans: dict[str, Plan] = field(default_factory=dict)
    last_refreshed_at: datetime | None = None
    scheduler: Scheduler | None = None
    audit_repository: AuditEventRepository | None = None


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
                context.last_refreshed_at = datetime.now(UTC)
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
            energy_context = context.energy_context_provider.get_context(requested_horizon)
            parsed_context = EnergyContext.model_validate(energy_context)
            if parsed_context.horizon != requested_horizon:
                raise ValueError("Energy context horizon does not match the request")
            return energy_context_snapshot(
                parsed_context,
                context.discovery.state_store.runtime_revision,
            )
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="validate_command",
        description="Validate one semantic command without invoking an adapter.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def validate_command(command: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed_command = Command.model_validate(command)
            plan = Plan(id=f"command-validation-{parsed_command.id}", commands=[parsed_command])
            validated = context.facade.validate_plan(plan)
            context.plans[validated.id] = validated
            return {
                "schema_version": "v1",
                "plan_id": validated.id,
                "command": parsed_command.model_dump(mode="json"),
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
            validated = context.facade.validate_plan(parsed_plan)
            context.plans[validated.id] = validated
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
            "confirmation. Requires the deployment's operator_token; an "
            "automated agent that does not hold it cannot self-approve. The "
            "returned approval_id is single-use and bound to the plan's "
            "current validation digest."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    async def request_approval(
        plan_id: str,
        validation_digest: str,
        approved_by: str,
        operator_token: str,
    ) -> dict[str, Any]:
        try:
            plan = context.plans.get(plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            if plan.validation is None or plan.validation.digest != validation_digest:
                raise ValueError("Validation digest does not match the stored plan")
            grant = context.approval_store.issue(
                plan, approved_by=approved_by, operator_token=operator_token
            )
            return {
                "schema_version": "v1",
                "approval_id": grant.approval_id,
                "plan_id": grant.plan_id,
                "validation_digest": grant.validation_digest,
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
        dry_run: bool = False,
    ) -> dict[str, Any]:
        try:
            plan = context.plans.get(plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            if plan.validation is None or plan.validation.digest != validation_digest:
                raise ValueError("Validation digest does not match the stored plan")
            if plan.status is PlanStatus.REQUIRES_CONFIRMATION:
                if approval_id is None:
                    raise ValueError("Plan requires an approval_id issued via request_approval")
                grant = context.approval_store.consume(approval_id, plan)
                plan = context.facade.approve_plan(plan, grant=grant)
                context.plans[plan.id] = plan
            if dry_run:
                return {
                    "schema_version": "v1",
                    "dry_run": True,
                    "plan": plan.model_dump(mode="json"),
                }
            execution = await context.facade.execute_plan(plan)
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
    ) -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = context.plans.get(plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            if plan.validation is None or plan.validation.digest != validation_digest:
                raise ValueError("Validation digest does not match the stored plan")
            if plan.status is PlanStatus.REQUIRES_CONFIRMATION:
                if approval_id is None:
                    raise ValueError("Plan requires an approval_id issued via request_approval")
                grant = context.approval_store.consume(approval_id, plan)
                plan = context.facade.approve_plan(plan, grant=grant)
            scheduled_plan = plan.model_copy(
                update={"execute_at": datetime.fromisoformat(execute_at)}
            )
            context.plans[plan_id] = scheduled_plan
            await context.scheduler.schedule(scheduled_plan)
            return {
                "schema_version": "v1",
                "plan_id": scheduled_plan.id,
                "execute_at": scheduled_plan.execute_at.isoformat()
                if scheduled_plan.execute_at
                else None,
            }
        except (ValueError, ValidationError) as error:
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
            cancelled = await context.scheduler.cancel(plan_id)
            return {"schema_version": "v1", "plan_id": plan_id, "cancelled": cancelled}
        except (ValueError, ValidationError) as error:
            return error_envelope(error)

    @server.tool(
        name="reschedule_plan",
        description="Move a pending scheduled plan to a different future time.",
        annotations=mutation_annotations,
        structured_output=True,
    )
    async def reschedule_plan(plan_id: str, execute_at: str) -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            rescheduled = await context.scheduler.reschedule(
                plan_id, datetime.fromisoformat(execute_at)
            )
            return {"schema_version": "v1", "plan_id": plan_id, "rescheduled": rescheduled}
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
            "time, optionally restricted to specific weekdays. Every "
            "occurrence is independently revalidated against live state "
            "before it executes; an occurrence requiring confirmation is "
            "skipped and audited, never auto-approved, and recurrence "
            "continues to its next scheduled time."
        ),
        annotations=mutation_annotations,
        structured_output=True,
    )
    async def schedule_recurring_plan(
        plan_id: str,
        time_of_day: str,
        timezone: str,
        days_of_week: list[int] | None = None,
    ) -> dict[str, Any]:
        try:
            if context.scheduler is None:
                raise ValueError("Scheduling is unavailable in this deployment")
            plan = context.plans.get(plan_id)
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            hour, minute = (int(part) for part in time_of_day.split(":"))
            rule = RecurrenceRule(
                time_of_day=time(hour=hour, minute=minute),
                timezone=timezone,
                days_of_week=days_of_week,
            )
            schedule_id = f"recurring:{plan_id}:{datetime.now(UTC).isoformat()}"
            first_occurrence = await context.scheduler.schedule_recurring(
                schedule_id, plan.commands, rule
            )
            return {
                "schema_version": "v1",
                "schedule_id": schedule_id,
                "next_execute_at": first_occurrence.isoformat(),
            }
        except (ValueError, ValidationError) as error:
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
                since=datetime.fromisoformat(since) if since is not None else None,
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
