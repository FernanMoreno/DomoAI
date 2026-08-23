"""Configurable composition root for fixture and live runtime deployments."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.adapters.home_assistant.config import load_home_assistant_mapping
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import load_mapping
from domoai.adapters.knx.transport import XknxTransport
from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.transport import MatterServerWebSocketTransport
from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.config import load_mapping as load_modbus_mapping
from domoai.adapters.modbus.transport import PyModbusTcpTransport
from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.adapters.zigbee2mqtt.transport import AiomqttTransport
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.config.battery_profile import load_dispatchable_battery_binding
from domoai.config.battery_qualification import (
    BatteryQualificationError,
    load_battery_hil_evidence,
)
from domoai.config.policy_loader import load_policy_file
from domoai.config.risk_classification import load_risk_overrides_file
from domoai.config.safety_kernel_loader import load_safety_limits_file
from domoai.config.settings import Settings
from domoai.config.solar_profile import resolve_solar_profile
from domoai.domain.models import Plan
from domoai.optimizer.omie import OmieTariffHttpClient, OmieTariffProvider
from domoai.optimizer.open_meteo import (
    OpenMeteoHttpClient,
    OpenMeteoSolarConfig,
    OpenMeteoSolarProvider,
)
from domoai.optimizer.ports import EnergyContextProvider
from domoai.optimizer.providers import (
    BatteryProvider,
    ComposedEnergyContextProvider,
    DispatchableBatteryBinding,
    StateStoreBatteryProvider,
)
from domoai.persistence.repositories import (
    AuditEventRepository,
    BundleCommitRepository,
    DeviceRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
    RecurringScheduleRepository,
    RuntimeStateMetadataRepository,
    RuntimeStatePersistenceRepository,
    ScheduledPlanRepository,
    StateSnapshotRepository,
)
from domoai.persistence.serialized import SerializedRepositoryProxy, SerializedStorageExecutor
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import (
    ApprovalStore,
    OperatorApprovalAssertionProvider,
    OperatorPrincipalProvider,
)
from domoai.runtime.bundle_commit import BundleCommitService, BundleRecoveryService
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.control_takeover import BatteryControlCoordinator
from domoai.runtime.event_consumer import RuntimeEventConsumer
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.ports import AdapterPort
from domoai.runtime.provider_sdk import ProviderRegistry
from domoai.runtime.recovery import PlanRecoveryService
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.risk_classifier import RiskClassifier
from domoai.runtime.safety_kernel import SafetyKernel
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore


def create_adapter(
    settings: Settings,
    *,
    registry: DeviceRegistry | None = None,
    provider_registry: ProviderRegistry | None = None,
    clock: Clock | None = None,
) -> AdapterPort:
    adapters = _create_configured_adapters(
        settings, provider_registry=provider_registry, clock=clock
    )
    if not adapters:
        return SimulatedHomeAdapter(clock=clock)
    if len(adapters) == 1:
        return adapters[0]
    return CompositeAdapter(
        adapters,
        registry=registry,
        event_queue_max_size=settings.composite_event_queue_max_size,
        reconnect_on_stream_end=True,
    )


def _create_configured_adapters(
    settings: Settings,
    *,
    provider_registry: ProviderRegistry | None = None,
    clock: Clock | None = None,
) -> list[AdapterPort]:
    adapters: list[AdapterPort] = []
    has_url = settings.home_assistant_url is not None
    has_token = settings.home_assistant_token is not None
    if has_url != has_token:
        raise ValueError(
            "DOMOAI_HOME_ASSISTANT_URL and DOMOAI_HOME_ASSISTANT_TOKEN must be configured together"
        )
    if has_url and has_token:
        assert settings.home_assistant_url is not None
        assert settings.home_assistant_token is not None
        mapping_document = (
            load_home_assistant_mapping(settings.home_assistant_mapping_path)
            if settings.home_assistant_mapping_path is not None
            else None
        )
        client = HomeAssistantClient(
            settings.home_assistant_url,
            settings.home_assistant_token.get_secret_value(),
        )
        provider = HomeAssistantProvider(
            client,
            metric_mappings=(mapping_document.metric_mappings if mapping_document else None),
            battery_capacity_bindings=(
                mapping_document.battery_capacity_bindings if mapping_document else None
            ),
            battery_dispatch_bindings=(
                mapping_document.battery_dispatch_bindings if mapping_document else None
            ),
            clock=clock,
        )
        if provider_registry is not None:
            provider_registry.register(provider)
        adapters.append(HomeAssistantProviderAdapter(provider, clock=clock))
    if settings.zigbee2mqtt_url is not None:
        parsed = urlparse(settings.zigbee2mqtt_url)
        if parsed.scheme not in {"mqtt", "mqtts"} or parsed.hostname is None:
            raise ValueError("DOMOAI_ZIGBEE2MQTT_URL must be a valid mqtt:// or mqtts:// URL")
        use_tls = parsed.scheme == "mqtts"
        default_port = 8883 if use_tls else 1883
        adapters.append(
            Zigbee2MqttAdapter(
                AiomqttTransport(
                    parsed.hostname,
                    port=parsed.port or default_port,
                    username=settings.mqtt_username,
                    password=(
                        settings.mqtt_password.get_secret_value()
                        if settings.mqtt_password is not None
                        else None
                    ),
                    timeout=settings.mqtt_timeout_seconds,
                    tls=use_tls,
                    ca_cert_path=settings.mqtt_ca_cert_path,
                    client_cert_path=settings.mqtt_client_cert_path,
                    client_key_path=settings.mqtt_client_key_path,
                    tls_insecure=settings.mqtt_tls_insecure,
                ),
                base_topic=settings.zigbee2mqtt_base_topic,
                discovery_timeout=settings.mqtt_timeout_seconds,
                clock=clock,
            )
        )
    if settings.matter_server_url is not None:
        parsed = urlparse(settings.matter_server_url)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname is None:
            raise ValueError("DOMOAI_MATTER_SERVER_URL must be a valid ws:// or wss:// URL")
        adapters.append(
            MatterServerAdapter(
                MatterServerWebSocketTransport(
                    settings.matter_server_url,
                    timeout=settings.matter_timeout_seconds,
                ),
                discovery_timeout=settings.matter_timeout_seconds,
                clock=clock,
            )
        )
    if settings.knx_gateway_host is not None and settings.knx_config_path is not None:
        mapping = load_mapping(Path(settings.knx_config_path))
        group_dpts = {
            binding.state_group_address: binding.dpt
            for entity in mapping.entities
            for binding in entity.capabilities
        }
        adapters.append(
            KnxAdapter(
                XknxTransport(
                    settings.knx_gateway_host,
                    timeout=settings.knx_timeout_seconds,
                    group_dpts=group_dpts,
                    clock=clock,
                ),
                mapping,
                discovery_timeout=settings.knx_timeout_seconds,
                clock=clock,
            ),
        )
    if settings.modbus_host is not None and settings.modbus_config_path is not None:
        if not settings.modbus_host.strip():
            raise ValueError("DOMOAI_MODBUS_HOST must not be empty")
        modbus_mapping = load_modbus_mapping(Path(settings.modbus_config_path))
        adapters.append(
            ModbusAdapter(
                PyModbusTcpTransport(
                    settings.modbus_host,
                    port=settings.modbus_port,
                    timeout=settings.modbus_timeout_seconds,
                    clock=clock,
                ),
                modbus_mapping,
                discovery_timeout=settings.modbus_timeout_seconds,
                poll_interval=settings.modbus_poll_interval_seconds,
                clock=clock,
            ),
        )
    return adapters


def _create_energy_context_provider(
    settings: Settings,
    *,
    battery_provider: BatteryProvider | None = None,
    clock: Clock | None = None,
) -> tuple[EnergyContextProvider | None, tuple[Callable[[], None], ...]]:
    if not settings.energy_live:
        return None, ()
    if settings.tariff_provider != "omie" or settings.solar_provider != "open_meteo":
        raise ValueError("unsupported live energy provider selection")
    profile = resolve_solar_profile(
        profile_path=settings.solar_profile_path,
        latitude=settings.solar_latitude,
        longitude=settings.solar_longitude,
        installed_kwp=settings.solar_installed_kwp,
        tilt=settings.solar_tilt,
        azimuth=settings.solar_azimuth,
        performance_ratio=settings.solar_performance_ratio,
        inverter_ac_max_kw=settings.solar_inverter_ac_max_kw,
        timezone=settings.solar_timezone,
    )
    solar_config = OpenMeteoSolarConfig.from_profile(profile)
    omie_client = OmieTariffHttpClient(timeout=settings.omie_timeout_seconds, clock=clock)
    solar_client = OpenMeteoHttpClient(timeout=settings.solar_timeout_seconds, clock=clock)
    provider = ComposedEnergyContextProvider(
        tariffs=OmieTariffProvider(omie_client),
        solar=OpenMeteoSolarProvider(solar_client, solar_config),
        battery=battery_provider,
        max_age_seconds=settings.energy_max_age_seconds,
        now=(clock.now if clock is not None else None),
    )
    return provider, (omie_client.close, solar_client.close)


@dataclass
class RuntimeComposition:
    settings: Settings
    adapter: AdapterPort
    database: SQLiteDatabase
    storage: SerializedStorageExecutor
    audit_repository: AuditEventRepository
    plan_repository: PlanRepository
    outcome_repository: ExecutionOutcomeRepository
    device_repository: DeviceRepository
    state_snapshot_repository: StateSnapshotRepository
    runtime_state_metadata_repository: RuntimeStateMetadataRepository
    scheduled_plan_repository: ScheduledPlanRepository
    bundle_commit_repository: BundleCommitRepository
    bundle_commit_service: BundleCommitService
    approval_store: ApprovalStore
    plans: dict[str, Plan]
    recurring_schedule_repository: RecurringScheduleRepository
    registry: DeviceRegistry
    provider_registry: ProviderRegistry
    state_store: StateStore
    audit: AuditLog
    discovery: DiscoveryService
    plan_service: PlanService
    facade: DomoticsFacade
    event_consumer: RuntimeEventConsumer
    scheduler: Scheduler
    energy_context_provider: EnergyContextProvider | None = None
    battery_provider: BatteryProvider | None = None
    energy_closers: tuple[Callable[[], None], ...] = ()
    clock: Clock = field(default_factory=SystemClock)
    operator_principal_provider: OperatorPrincipalProvider | None = None
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None
    battery_qualification: str = "unsupported"

    async def close(self) -> None:
        try:
            await self.adapter.disconnect()
        finally:
            for close in self.energy_closers:
                close()
            await self.storage.close()
            await self.database.close()


async def build_runtime(
    settings: Settings | None = None,
    *,
    adapter: AdapterPort | None = None,
    dispatchable_battery_binding: DispatchableBatteryBinding | None = None,
    operator_principal_provider: OperatorPrincipalProvider | None = None,
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None,
    clock: Clock | None = None,
) -> RuntimeComposition:
    resolved_settings = settings or Settings.from_environment()
    configured_battery_binding = (
        load_dispatchable_battery_binding(resolved_settings.battery_dispatch_profile_path)
        if resolved_settings.battery_dispatch_profile_path is not None
        else None
    )
    if configured_battery_binding is not None and dispatchable_battery_binding is not None:
        raise ValueError(
            "battery dispatch binding must be supplied either by profile path or argument"
        )
    dispatchable_battery_binding = configured_battery_binding or dispatchable_battery_binding
    battery_qualification = "unsupported"
    if dispatchable_battery_binding is not None:
        battery_qualification = "software-qualified"
        if resolved_settings.battery_hil_evidence_path is not None:
            evidence = load_battery_hil_evidence(resolved_settings.battery_hil_evidence_path)
            if evidence.qualifies(dispatchable_battery_binding):
                battery_qualification = "hil-qualified"
        if resolved_settings.battery_dispatch_production and battery_qualification != (
            "hil-qualified"
        ):
            raise BatteryQualificationError(
                "production battery dispatch requires passing matching HIL evidence"
            )
    elif (
        resolved_settings.battery_dispatch_production
        or resolved_settings.battery_hil_evidence_path is not None
    ):
        raise BatteryQualificationError(
            "battery HIL evidence/production mode requires a dispatchable battery binding"
        )
    if dispatchable_battery_binding is not None and not resolved_settings.energy_live:
        raise ValueError("dispatchable_battery_binding requires energy_live to be enabled")
    clock = clock or SystemClock()
    state_store = StateStore(
        stale_after=timedelta(seconds=resolved_settings.state_stale_after_seconds),
        clock=clock,
    )
    battery_provider = (
        StateStoreBatteryProvider.from_binding(
            state_store=state_store,
            binding=dispatchable_battery_binding,
        )
        if dispatchable_battery_binding is not None
        else None
    )
    energy_context_provider, energy_closers = _create_energy_context_provider(
        resolved_settings,
        battery_provider=battery_provider,
        clock=clock,
    )
    database = SQLiteDatabase(
        resolved_settings.database_path,
        busy_timeout_ms=resolved_settings.sqlite_busy_timeout_ms,
        clock=clock,
    )
    storage = SerializedStorageExecutor(
        queue_capacity=resolved_settings.sqlite_worker_queue_capacity,
        queue_wait_seconds=resolved_settings.sqlite_worker_queue_wait_seconds,
        operation_timeout_seconds=resolved_settings.sqlite_operation_timeout_seconds,
    )
    await storage.run_async(database.initialize)
    raw_audit_repository = AuditEventRepository(database)
    raw_device_repository = DeviceRepository(database, clock=clock)
    raw_state_snapshot_repository = StateSnapshotRepository(database)
    raw_runtime_state_metadata_repository = RuntimeStateMetadataRepository(database, clock=clock)
    raw_state_persistence_repository = RuntimeStatePersistenceRepository(database, clock=clock)
    audit_repository = cast(
        AuditEventRepository, SerializedRepositoryProxy(raw_audit_repository, storage)
    )
    device_repository = cast(
        DeviceRepository, SerializedRepositoryProxy(raw_device_repository, storage)
    )
    state_snapshot_repository = cast(
        StateSnapshotRepository, SerializedRepositoryProxy(raw_state_snapshot_repository, storage)
    )
    runtime_state_metadata_repository = cast(
        RuntimeStateMetadataRepository,
        SerializedRepositoryProxy(raw_runtime_state_metadata_repository, storage),
    )
    state_persistence_repository = SerializedRepositoryProxy(
        raw_state_persistence_repository, storage
    )
    audit = AuditLog(sink=audit_repository, clock=clock)
    state_store.bind_persistence(state_persistence_repository)
    registry = DeviceRegistry()
    registry.load_persisted(await device_repository.list_all())
    provider_registry = ProviderRegistry(clock=clock)
    selected_adapter = adapter or create_adapter(
        resolved_settings,
        registry=registry,
        provider_registry=provider_registry,
        clock=clock,
    )
    if isinstance(selected_adapter, CompositeAdapter):
        selected_adapter.bind_registry(registry)
    runtime_state_metadata = await runtime_state_metadata_repository.get()
    if runtime_state_metadata is not None:
        state_store.restore_metadata(runtime_state_metadata)
    state_store.load_persisted(await state_snapshot_repository.list_all())
    discovery = DiscoveryService(
        selected_adapter,
        registry,
        state_store,
        audit,
        device_repository=device_repository,
        state_snapshot_repository=state_snapshot_repository,
        runtime_state_metadata_repository=runtime_state_metadata_repository,
        clock=clock,
    )
    try:
        await selected_adapter.connect()
        await discovery.refresh()
    except (ConnectionError, OSError) as error:
        audit.append(
            event_type="runtime_started_degraded",
            actor="runtime",
            subject_id=selected_adapter.adapter_id,
            payload={"error": str(error)},
        )
    if resolved_settings.policy_config_path is not None:
        policies = load_policy_file(resolved_settings.policy_config_path)
    else:
        policies = []
        audit.append(
            event_type="policy_default_applied",
            actor="runtime",
            subject_id="build_runtime",
            payload={"reason": "no policy_config_path configured"},
        )
    risk_overrides = (
        load_risk_overrides_file(resolved_settings.risk_overrides_path)
        if resolved_settings.risk_overrides_path is not None
        else []
    )
    risk_classifier = RiskClassifier(overrides=tuple(risk_overrides))
    policy_engine = PolicyEngine(policies, risk_classifier)
    plan_service = PlanService(registry, state_store, policy_engine, audit, clock=clock)
    plan_repository = cast(
        PlanRepository, SerializedRepositoryProxy(PlanRepository(database, clock=clock), storage)
    )
    await PlanRecoveryService(plan_repository, audit).recover_orphaned_plans()
    outcome_repository = cast(
        ExecutionOutcomeRepository,
        SerializedRepositoryProxy(ExecutionOutcomeRepository(database), storage),
    )
    if resolved_settings.safety_limits_path is not None:
        safety_limits = load_safety_limits_file(resolved_settings.safety_limits_path)
    else:
        safety_limits = []
        audit.append(
            event_type="safety_kernel_default_applied",
            actor="runtime",
            subject_id="build_runtime",
            payload={"reason": "no safety_limits_path configured"},
        )
    safety_kernel = SafetyKernel(safety_limits)
    executor = PlanExecutor(
        selected_adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        outcome_repository=outcome_repository,
        state_snapshot_repository=state_snapshot_repository,
        clock=clock,
        safety_kernel=safety_kernel,
        control_takeover=(
            BatteryControlCoordinator(
                selected_adapter,  # type: ignore[arg-type]
                dispatchable_battery_binding.control_policy,  # type: ignore[arg-type]
                device_id=dispatchable_battery_binding.device_id,
                command_names=frozenset(
                    {
                        dispatchable_battery_binding.profile.actuator.charge_command,
                        dispatchable_battery_binding.profile.actuator.discharge_command,
                        dispatchable_battery_binding.profile.actuator.stop_command,
                    }
                ),
                clock=clock,
            )
            if dispatchable_battery_binding is not None
            and dispatchable_battery_binding.profile.actuator is not None
            else None
        ),
    )
    facade = DomoticsFacade(plan_service, executor)
    event_consumer = RuntimeEventConsumer(
        selected_adapter, discovery, state_store, audit, clock=clock
    )
    scheduled_plan_repository = cast(
        ScheduledPlanRepository,
        SerializedRepositoryProxy(ScheduledPlanRepository(database, clock=clock), storage),
    )
    bundle_commit_repository = cast(
        BundleCommitRepository,
        SerializedRepositoryProxy(BundleCommitRepository(database, clock=clock), storage),
    )
    recurring_schedule_repository = cast(
        RecurringScheduleRepository,
        SerializedRepositoryProxy(RecurringScheduleRepository(database, clock=clock), storage),
    )
    scheduler = Scheduler(
        executor,
        scheduled_plan_repository,
        audit,
        grace_window=timedelta(seconds=resolved_settings.scheduler_grace_window_seconds),
        poll_interval=timedelta(seconds=resolved_settings.scheduler_poll_interval_seconds),
        recurring_repository=recurring_schedule_repository,
        bundle_repository=bundle_commit_repository,
        clock=clock,
    )
    plans: dict[str, Plan] = {}
    approval_store = ApprovalStore(
        operator_token=(
            resolved_settings.operator_approval_token.get_secret_value()
            if resolved_settings.operator_approval_token is not None
            else None
        ),
        allow_legacy_token=resolved_settings.allow_legacy_operator_token,
        clock=clock,
    )
    bundle_commit_service = BundleCommitService(
        facade=facade,
        plans=plans,
        approval_store=approval_store,
        bundle_repository=bundle_commit_repository,
        scheduled_repository=scheduled_plan_repository,
        audit=audit,
        plan_repository=plan_repository,
        clock=clock,
    )
    await BundleRecoveryService(
        bundle_repository=bundle_commit_repository,
        plan_repository=plan_repository,
        scheduled_repository=scheduled_plan_repository,
        audit=audit,
    ).recover_orphaned_bundles()
    return RuntimeComposition(
        settings=resolved_settings,
        adapter=selected_adapter,
        database=database,
        storage=storage,
        audit_repository=audit_repository,
        plan_repository=plan_repository,
        outcome_repository=outcome_repository,
        device_repository=device_repository,
        state_snapshot_repository=state_snapshot_repository,
        runtime_state_metadata_repository=runtime_state_metadata_repository,
        scheduled_plan_repository=scheduled_plan_repository,
        bundle_commit_repository=bundle_commit_repository,
        bundle_commit_service=bundle_commit_service,
        approval_store=approval_store,
        plans=plans,
        recurring_schedule_repository=recurring_schedule_repository,
        registry=registry,
        provider_registry=provider_registry,
        state_store=state_store,
        audit=audit,
        discovery=discovery,
        plan_service=plan_service,
        facade=facade,
        event_consumer=event_consumer,
        scheduler=scheduler,
        energy_context_provider=energy_context_provider,
        battery_provider=battery_provider,
        energy_closers=energy_closers,
        clock=clock,
        operator_principal_provider=operator_principal_provider,
        operator_approval_assertion_provider=operator_approval_assertion_provider,
        battery_qualification=battery_qualification,
    )
