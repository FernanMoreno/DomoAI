"""Shared application facade for MCP and direct callers."""

from __future__ import annotations

from domoai.application.plan_service import PlanService
from domoai.domain.models import ExecutionSummary, Plan
from domoai.runtime.approval_store import ApprovalGrant
from domoai.runtime.executor import PlanExecutor


class DomoticsFacade:
    def __init__(self, plan_service: PlanService, executor: PlanExecutor) -> None:
        self.plan_service = plan_service
        self.executor = executor

    def validate_plan(self, plan: Plan) -> Plan:
        return self.plan_service.validate(plan)

    def approve_plan(self, plan: Plan, *, grant: ApprovalGrant) -> Plan:
        return self.plan_service.approve(plan, grant=grant)

    async def execute_plan(
        self, plan: Plan, *, state_version_overrides: dict[str, int] | None = None
    ) -> ExecutionSummary:
        return await self.executor.execute(
            plan, state_version_overrides=state_version_overrides
        )
