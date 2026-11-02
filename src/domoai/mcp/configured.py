"""Configured semantic server builder shared by stdio and HTTP entrypoints."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from domoai.application.authority import AuthorityPolicy
from domoai.application.metrics import RuntimeMetricsCollector
from domoai.application.optimization_service import OptimizationService
from domoai.application.optimization_worker import OptimizationWorker, WorkerBudget
from domoai.application.privacy import PrivacyService
from domoai.application.process_optimization_worker import ProcessOptimizationWorker
from domoai.application.runtime_factory import RuntimeComposition, build_runtime
from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.domain.energy import EVChargingBinding
from domoai.domain.models import AuthorityContext, PrincipalRole
from domoai.domain.privacy import HouseholdDataPolicy, PrivacyCategory
from domoai.mcp.auth import StaticBearerTokenVerifier
from domoai.mcp.domotics_server import DomoticsMcpContext
from domoai.mcp.ortools_server import OrtoolsMcpContext
from domoai.mcp.unified_server import UnifiedMcpContext, create_unified_server
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.ports import EnergyContextProvider
from domoai.persistence.privacy import SQLitePrivacyStore
from domoai.runtime.approval_store import (
    OperatorApprovalAssertionProvider,
    OperatorPrincipalProvider,
)


def _adapter_ids(adapter: object) -> tuple[str, ...]:
    """Return concrete adapter identities without exposing provider secrets."""

    child_attribute = getattr(adapter, "adapters", None)
    if child_attribute is not None:
        children = tuple(child_attribute)
        if not children:
            return ()
        ids = {child_id for child in children for child_id in _adapter_ids(child)}
    else:
        adapter_id = getattr(adapter, "adapter_id", None)
        ids = {str(adapter_id)} if adapter_id else set()
    return tuple(sorted(ids))


async def build_configured_server(
    settings: Settings | None = None,
    *,
    operator_principal_provider: OperatorPrincipalProvider | None = None,
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None,
    energy_context_provider: EnergyContextProvider | None = None,
    ev_charging_bindings: tuple[EVChargingBinding, ...] = (),
    require_configured_adapter: bool = False,
    control_plane_mode: bool = False,
    client_token_verifier: StaticBearerTokenVerifier | None = None,
) -> tuple[RuntimeComposition, FastMCP]:
    resolved_settings = settings or Settings.from_environment()
    if resolved_settings.mcp_chat_confirmation and (
        operator_principal_provider is not None or operator_approval_assertion_provider is not None
    ):
        raise ValueError("chat confirmation cannot be combined with operator providers")
    # Validate deployment credentials before build_runtime opens SQLite,
    # claims ownership or connects a provider. The server builder validates
    # again when it creates the auth middleware; this early check prevents a
    # malformed launcher configuration from leaking a partially-built runtime.
    if client_token_verifier is not None:
        # Same early failure for an inconsistent injection; the gateway already
        # loaded this verifier from the configured file, so it is not re-read.
        if (
            type(client_token_verifier) is not StaticBearerTokenVerifier
            or resolved_settings.mcp_client_token_file is None
        ):
            raise ValueError("an injected client token verifier requires a configured token file")
    elif resolved_settings.mcp_client_token_file is not None:
        StaticBearerTokenVerifier.from_file(resolved_settings.mcp_client_token_file)
    runtime = await build_runtime(
        resolved_settings,
        operator_principal_provider=operator_principal_provider,
        operator_approval_assertion_provider=operator_approval_assertion_provider,
        energy_context_provider=energy_context_provider,
        ev_charging_bindings=ev_charging_bindings,
        require_configured_adapter=require_configured_adapter,
        control_plane_mode=control_plane_mode,
    )
    if resolved_settings.mcp_chat_confirmation and not runtime.approval_store.durable:
        await runtime.close()
        raise ValueError("chat confirmation requires durable approval storage")
    optimization_service = OptimizationService(
        runtime.registry,
        runtime.plan_service,
        CpSatOptimizer(runtime.registry),
    )
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
            max_horizon_slots=runtime.settings.optimization_max_horizon_slots,
        )
    )
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
        operational_metrics=runtime.operational_metrics,
        metric_history_repository=runtime.metric_history_repository,
        metric_history_max_samples=runtime.settings.metric_history_max_samples,
        household_work_queues=runtime.household_work_queues,
    )
    context = DomoticsMcpContext(
        household_preferences=runtime.household_preferences,
        discovery=runtime.discovery,
        state_service=StateService(runtime.state_store),
        facade=runtime.facade,
        registry=runtime.registry,
        policies=runtime.plan_service.policy_engine.policies,
        active_provider_ids_supplier=lambda: _adapter_ids(runtime.adapter),
        battery_qualification=runtime.battery_qualification,
        plan_repository=runtime.plan_repository,
        state_history_repository=runtime.state_history_repository,
        approval_store=runtime.approval_store,
        plans=runtime.plans,
        energy_context_provider=runtime.energy_context_provider,
        scheduler=runtime.scheduler,
        audit_repository=runtime.audit_repository,
        metrics=metrics,
        bundle_commit_service=runtime.bundle_commit_service,
        operator_principal_provider=runtime.operator_principal_provider,
        operator_approval_assertion_provider=runtime.operator_approval_assertion_provider,
        chat_confirmation=runtime.settings.mcp_chat_confirmation,
        chat_confirmation_timeout_seconds=runtime.settings.mcp_chat_confirmation_timeout_seconds,
        chat_confirmation_timezone=runtime.settings.solar_timezone,
        blocking_worker=energy_worker,
        provider_timeout_seconds=runtime.settings.provider_worker_timeout_seconds,
        clock=runtime.clock,
        commissioning_service=runtime.commissioning_service,
        commissioning_report=runtime.commissioning_report,
        qualification_repository=runtime.qualification_repository,
        local_automation=runtime.local_automation,
        privacy_service=PrivacyService(
            SQLitePrivacyStore(runtime.database),
            audit=runtime.audit.append,
            state_store=runtime.state_store,
        ),
        privacy_policy=HouseholdDataPolicy(
            authority=AuthorityContext(
                tenant_id=runtime.settings.mcp_tenant_id,
                household_id=runtime.settings.mcp_household_id,
                household_ids=[runtime.settings.mcp_household_id],
                principal_id="runtime",
                roles=[PrincipalRole.SERVICE],
            ),
            exportable_categories=[
                PrivacyCategory.STATE,
                PrivacyCategory.PLANS,
                PrivacyCategory.APPROVALS,
                PrivacyCategory.BUNDLES,
                PrivacyCategory.SCHEDULES,
                PrivacyCategory.AUTOMATIONS,
            ],
            deletable_categories=[
                PrivacyCategory.STATE,
                PrivacyCategory.PLANS,
                PrivacyCategory.SCHEDULES,
                PrivacyCategory.AUTOMATIONS,
            ],
            immutable_categories=[PrivacyCategory.AUDIT],
            retention_days=runtime.settings.privacy_retention_days,
        ),
        authority_policy=AuthorityPolicy(
            tenant_id=runtime.settings.mcp_tenant_id,
            household_id=runtime.settings.mcp_household_id,
        ),
    )
    optimizer_context = OrtoolsMcpContext(
        registry=runtime.registry,
        plan_service=runtime.plan_service,
        optimization_service=optimization_service,
        optimization_worker=worker,
        max_horizon_slots=runtime.settings.optimization_max_horizon_slots,
        authority_policy=AuthorityPolicy(
            tenant_id=runtime.settings.mcp_tenant_id,
            household_id=runtime.settings.mcp_household_id,
        ),
    )
    return runtime, create_unified_server(
        UnifiedMcpContext(domotics=context, optimizer=optimizer_context),
        settings=runtime.settings,
        runtime=runtime,
        client_token_verifier=client_token_verifier,
    )
