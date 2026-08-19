"""Deterministic, isolated re-execution of a previously persisted plan."""

from __future__ import annotations

from pydantic import Field

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import ExecutionOutcome, PlanStatus, StrictModel
from domoai.persistence.repositories import PlanRepository
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


class ReplayResult(StrictModel):
    plan_id: str = Field(min_length=1)
    found: bool
    status: PlanStatus | None = None
    original_status: PlanStatus | None = None
    outcomes: list[ExecutionOutcome] = Field(default_factory=list)
    incomplete_reconstruction_notes: list[str] = Field(default_factory=list)


class PlanReplayer:
    """Re-validates and re-executes a persisted plan against an isolated fixture.

    The replay registry is built by discovering a fresh, isolated fixture
    adapter rather than restoring persisted device records directly:
    ``DeviceRegistry.load_persisted`` deliberately does not rebuild command
    routes (see its docstring), so a plan validated against a
    load_persisted-only registry would always fail route resolution. Only a
    discovery pass builds a registry that can actually execute commands, and
    since replay must never contact a real or live-shared adapter (FR-003),
    that discovery targets a fresh fixture instance instead.
    """

    def __init__(self, plan_repository: PlanRepository) -> None:
        self.plan_repository = plan_repository

    async def replay(self, plan_id: str) -> ReplayResult:
        plan = await self.plan_repository.get(plan_id)
        if plan is None:
            return ReplayResult(plan_id=plan_id, found=False)

        registry = DeviceRegistry()
        state_store = StateStore()
        audit = AuditLog()
        adapter = SimulatedHomeAdapter()
        await DiscoveryService(adapter, registry, state_store, audit).refresh()

        notes = [
            f"device {command.device_id!r} not available in the replay fixture"
            for command in plan.commands
            if registry.get(command.device_id) is None
        ]

        clock = FixedClock(plan.execute_at or plan.created_at)
        policy_engine = PolicyEngine([])
        plan_service = PlanService(registry, state_store, policy_engine, audit, clock=clock)
        executor = PlanExecutor(adapter, plan_service, audit, clock=clock)

        validated = plan_service.validate(plan)
        outcomes: list[ExecutionOutcome] = []
        status = validated.status
        if validated.status is PlanStatus.READY:
            summary = await executor.execute(validated)
            outcomes = summary.outcomes
            status = PlanExecutor._terminal_plan_status(outcomes)

        return ReplayResult(
            plan_id=plan_id,
            found=True,
            status=status,
            original_status=plan.status,
            outcomes=outcomes,
            incomplete_reconstruction_notes=notes,
        )
