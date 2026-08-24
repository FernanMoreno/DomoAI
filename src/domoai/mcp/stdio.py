"""Local stdio entry point backed by the deterministic simulated home."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from mcp.server.fastmcp import FastMCP

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.optimization_service import OptimizationService
from domoai.application.optimization_worker import OptimizationWorker, WorkerBudget
from domoai.application.plan_service import PlanService
from domoai.application.process_optimization_worker import ProcessOptimizationWorker
from domoai.application.runtime_factory import RuntimeComposition, build_runtime
from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.mcp.domotics_server import DomoticsMcpContext
from domoai.mcp.ortools_server import OrtoolsMcpContext
from domoai.mcp.unified_server import UnifiedMcpContext, create_unified_server
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.runtime.approval_store import (
    OperatorApprovalAssertionProvider,
    OperatorPrincipalProvider,
)
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.metrics import RuntimeMetricsCollector
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def build_fixture_server() -> FastMCP:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=plan_service.policy_engine.policies,
    )
    optimizer_context = OrtoolsMcpContext(
        registry=registry,
        plan_service=plan_service,
        optimization_service=OptimizationService(
            registry,
            plan_service,
            CpSatOptimizer(registry),
        ),
    )
    return create_unified_server(UnifiedMcpContext(domotics=context, optimizer=optimizer_context))


async def build_configured_server(
    settings: Settings | None = None,
    *,
    operator_principal_provider: OperatorPrincipalProvider | None = None,
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None,
) -> tuple[RuntimeComposition, FastMCP]:
    runtime = await build_runtime(
        settings,
        operator_principal_provider=operator_principal_provider,
        operator_approval_assertion_provider=operator_approval_assertion_provider,
    )
    optimization_service = OptimizationService(
        runtime.registry,
        runtime.plan_service,
        CpSatOptimizer(runtime.registry),
    )
    # CP-SAT-facing worker: process-backed (spec 150), not thread-backed.
    # A timed-out solve's OS process is genuinely terminated instead of
    # merely abandoned -- see specs/150-cp-sat-process-isolation/research.md
    # for why a thread can't provide that guarantee. optimization_service
    # itself is still used directly (validate_proposal, not through this
    # worker) by mcp/ortools_server.py's optimize_scenario tool.
    worker = runtime.register_blocking_worker(
        ProcessOptimizationWorker(
            runtime.registry,
            WorkerBudget(
                max_solver_time_seconds=runtime.settings.optimization_max_solver_time_seconds,
                queue_capacity=runtime.settings.optimization_worker_queue_capacity,
                max_concurrency=runtime.settings.optimization_worker_concurrency,
                queue_wait_seconds=runtime.settings.optimization_worker_queue_wait_seconds,
                provider_timeout_seconds=runtime.settings.provider_worker_timeout_seconds,
            ),
        )
    )
    # A second, separately-scoped worker for the energy-context provider
    # boundary -- distinct `service`, so it cannot share the optimizer
    # worker above (see DomoticsMcpContext.blocking_worker / spec 147).
    # Constructed eagerly and registered here rather than lazily inside the
    # get_energy_context tool, so `runtime.close()` actually owns it.
    energy_worker = (
        runtime.register_blocking_worker(OptimizationWorker(runtime.energy_context_provider))
        if runtime.energy_context_provider is not None
        else None
    )
    metrics = RuntimeMetricsCollector(
        adapter=runtime.adapter,
        event_consumer=runtime.event_consumer,
        scheduler=runtime.scheduler,
        state_store=runtime.state_store,
        plan_repository=runtime.plan_repository,
        database=runtime.database,
        storage=runtime.storage,
        audit_storage=runtime.audit_storage,
        audit=runtime.audit,
        battery_qualification=runtime.battery_qualification,
        optimization_worker=worker,
        optimization_service=optimization_service,
        clock=runtime.clock,
    )
    context = DomoticsMcpContext(
        discovery=runtime.discovery,
        state_service=StateService(runtime.state_store),
        facade=runtime.facade,
        registry=runtime.registry,
        policies=runtime.plan_service.policy_engine.policies,
        plan_repository=runtime.plan_repository,
        approval_store=runtime.approval_store,
        plans=runtime.plans,
        energy_context_provider=runtime.energy_context_provider,
        scheduler=runtime.scheduler,
        audit_repository=runtime.audit_repository,
        metrics=metrics,
        bundle_commit_service=runtime.bundle_commit_service,
        operator_principal_provider=runtime.operator_principal_provider,
        operator_approval_assertion_provider=runtime.operator_approval_assertion_provider,
        blocking_worker=energy_worker,
        provider_timeout_seconds=runtime.settings.provider_worker_timeout_seconds,
        clock=runtime.clock,
    )
    optimizer_context = OrtoolsMcpContext(
        registry=runtime.registry,
        plan_service=runtime.plan_service,
        optimization_service=optimization_service,
        optimization_worker=worker,
    )
    return runtime, create_unified_server(
        UnifiedMcpContext(domotics=context, optimizer=optimizer_context)
    )


async def run_stdio() -> None:
    runtime, server = await build_configured_server()
    event_task = asyncio.create_task(runtime.event_consumer.run())
    scheduler_task = asyncio.create_task(runtime.scheduler.run())
    try:
        await server.run_stdio_async()
    finally:
        event_task.cancel()
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await event_task
        with suppress(asyncio.CancelledError):
            await scheduler_task
        await runtime.close()


def main() -> None:
    asyncio.run(run_stdio())
