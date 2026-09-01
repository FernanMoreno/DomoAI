"""Configurable composition root for fixture and live runtime deployments."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol, TypeVar, cast
from urllib.parse import urlparse

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.adapters.home_assistant.config import (
    HomeAssistantEVChargingBinding,
    HomeAssistantMappingDocument,
    load_home_assistant_mapping,
)
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
from domoai.application.bundle_commit import BundleCommitService, BundleRecoveryService
from domoai.application.commissioning import (
    CommissioningPersistenceError,
    CommissioningService,
)
from domoai.application.discovery_service import DiscoveryService
from domoai.application.dynamic_safety import DynamicSafetyGuard
from domoai.application.event_consumer import RuntimeEventConsumer
from domoai.application.execution_admission import ExecutionAdmission
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.recovery import PlanRecoveryService
from domoai.application.runtime_bootstrap import RuntimeBootstrap, RuntimeBootstrapManifest
from domoai.application.runtime_lifecycle import RuntimeLifecycle
from domoai.application.runtime_ownership import RuntimeOwnership
from domoai.application.scheduler import Scheduler
from domoai.application.state_refresher import RuntimeStateRefresher
from domoai.config.battery_profile import load_dispatchable_battery_binding
from domoai.config.battery_qualification import (
    BatteryQualificationError,
    load_battery_hil_evidence,
)
from domoai.config.ev_charging_profile import load_ev_charging_binding
from domoai.config.policy_loader import load_policy_file
from domoai.config.risk_classification import load_risk_overrides_file
from domoai.config.safety_kernel_loader import load_safety_limits_file
from domoai.config.settings import Settings
from domoai.config.solar_profile import resolve_solar_profile
from domoai.domain.commissioning import CommissioningReport
from domoai.domain.energy import DispatchableBatteryBinding, EVActuator, EVChargingBinding
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
    EVProvider,
    StateStoreBatteryProvider,
    StateStoreEVProvider,
)
from domoai.persistence.backup import BackupManifest, BackupService, BackupSource
from domoai.persistence.repositories import (
    ApprovalGrantRepository,
    AuditEventRepository,
    BundleCommitRepository,
    DeviceRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
    RecurringScheduleRepository,
    RuntimeOwnershipRepository,
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
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.control_takeover import (
    BatteryControlCoordinator,
    ControlSupervisorPort,
    ControlTakeoverAdapter,
    ControlTakeoverGroup,
    EVControlCoordinator,
)
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import AdapterPort
from domoai.runtime.provider_sdk import ProviderRegistry
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.risk_classifier import RiskClassifier
from domoai.runtime.safety_kernel import SafetyKernel
from domoai.runtime.state_store import StateStore


def create_adapter(
    settings: Settings,
    *,
    registry: DeviceRegistry | None = None,
    provider_registry: ProviderRegistry | None = None,
    clock: Clock | None = None,
    dispatchable_battery_binding: DispatchableBatteryBinding | None = None,
    ev_charging_bindings: tuple[EVChargingBinding, ...] = (),
    require_configured_adapter: bool = False,
) -> AdapterPort:
    adapters = _create_configured_adapters(
        settings,
        provider_registry=provider_registry,
        clock=clock,
        dispatchable_battery_binding=dispatchable_battery_binding,
        ev_charging_bindings=ev_charging_bindings,
    )
    if not adapters:
        if require_configured_adapter:
            raise ValueError(
                "no configured adapter is available; refusing to select SimulatedHomeAdapter"
            )
        return SimulatedHomeAdapter(clock=clock)
    if len(adapters) == 1:
        return adapters[0]
    return CompositeAdapter(
        adapters,
        registry=registry,
        event_queue_max_size=settings.composite_event_queue_max_size,
        reconnect_on_stream_end=True,
    )


def _select_control_adapter(adapter: AdapterPort, provider_id: str) -> ControlTakeoverAdapter:
    """Route provider-specific takeover through a composite child."""

    candidate = _select_provider_adapter(adapter, provider_id)
    if not callable(getattr(candidate, "acquire_control", None)):
        raise ValueError(
            f"Battery provider {provider_id!r} does not expose the takeover contract"
        )
    return cast(ControlTakeoverAdapter, candidate)


def _select_provider_adapter(adapter: AdapterPort, provider_id: str) -> AdapterPort:
    """Resolve one concrete adapter for a server-owned provider identity."""

    candidate: object | None = None
    if getattr(adapter, "adapter_id", None) == provider_id:
        candidate = adapter
    for child in getattr(adapter, "adapters", ()):
        if getattr(child, "adapter_id", None) == provider_id:
            if candidate is not None:
                raise ValueError(f"Multiple adapters match battery provider {provider_id!r}")
            candidate = child
    if candidate is None:
        raise ValueError(
            f"Provider {provider_id!r} is not configured as a concrete runtime adapter"
        )
    return cast(AdapterPort, candidate)


def _matching_home_assistant_ev_mappings(
    mapping_document: HomeAssistantMappingDocument,
    bindings: tuple[EVChargingBinding, ...],
) -> dict[str, HomeAssistantEVChargingBinding]:
    """Select only HA route declarations for active canonical EV bindings."""

    mapping_bindings = getattr(mapping_document, "ev_charging_bindings", {})
    active_device_ids = {
        binding.device_id
        for binding in bindings
        if binding.provider_id == "home_assistant"
    }
    return {
        str(binding_id): binding
        for binding_id, binding in mapping_bindings.items()
        if binding.device_id in active_device_ids
        or binding.canonical_device_id in active_device_ids
    }


def _create_configured_adapters(
    settings: Settings,
    *,
    provider_registry: ProviderRegistry | None = None,
    clock: Clock | None = None,
    dispatchable_battery_binding: DispatchableBatteryBinding | None = None,
    ev_charging_bindings: tuple[EVChargingBinding, ...] = (),
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
                    mapping_document.battery_dispatch_bindings
                    if mapping_document is not None
                    and dispatchable_battery_binding is not None
                    and dispatchable_battery_binding.provider_id == "home_assistant"
                    else None
                ),
            ev_charging_bindings=(
                _matching_home_assistant_ev_mappings(mapping_document, ev_charging_bindings)
                if mapping_document is not None
                and any(binding.provider_id == "home_assistant" for binding in ev_charging_bindings)
                else None
            ),
            clock=clock,
        )
        if provider_registry is not None:
            provider_registry.register(provider)
        adapters.append(
            HomeAssistantProviderAdapter(
                provider,
                clock=clock,
                dispatchable_battery_binding=(
                    dispatchable_battery_binding
                    if dispatchable_battery_binding is not None
                    and dispatchable_battery_binding.provider_id == "home_assistant"
                    else None
                ),
                ev_charging_bindings=tuple(
                    binding
                    for binding in ev_charging_bindings
                    if binding.provider_id == "home_assistant"
                ),
            )
        )
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
                    gateway_port=settings.knx_gateway_port,
                    route_back=settings.knx_gateway_route_back,
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
    ev_providers: tuple[EVProvider, ...] = (),
    clock: Clock | None = None,
) -> tuple[EnergyContextProvider | None, tuple[Callable[[], None], ...]]:
    if not settings.energy_live:
        return None, ()
    # Spec 161: this equality check is the built-in-provider-only
    # requirement moved out of Settings.validate_source_selection --
    # Settings can no longer enforce it because it cannot know whether
    # build_runtime will receive an external energy_context_provider
    # override instead of reaching this function at all.
    if settings.tariff_provider != "omie":
        raise ValueError("DOMOAI_TARIFF_PROVIDER must be 'omie' when energy live mode is enabled")
    if settings.solar_provider != "open_meteo":
        raise ValueError(
            "DOMOAI_SOLAR_PROVIDER must be 'open_meteo' when energy live mode is enabled"
        )
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
        ev_providers=ev_providers,
        max_age_seconds=settings.energy_max_age_seconds,
        now=(clock.now if clock is not None else None),
    )
    return provider, (omie_client.close, solar_client.close)


class ClosableWorker(Protocol):
    """What `RuntimeComposition.close()` actually needs from a blocking
    worker (spec 150): thread- and process-backed workers (`OptimizationWorker`,
    `ProcessOptimizationWorker`) both satisfy this without either module
    importing the other."""

    def close(self) -> None: ...


_W = TypeVar("_W", bound=ClosableWorker)


@dataclass
class RuntimeComposition:
    settings: Settings
    adapter: AdapterPort
    database: SQLiteDatabase
    approval_database: SQLiteDatabase
    audit_database: SQLiteDatabase
    storage: SerializedStorageExecutor
    audit_storage: SerializedStorageExecutor
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
    state_refresher: RuntimeStateRefresher | None = None
    energy_context_provider: EnergyContextProvider | None = None
    battery_provider: BatteryProvider | None = None
    energy_closers: tuple[Callable[[], None], ...] = ()
    clock: Clock = field(default_factory=SystemClock)
    operator_principal_provider: OperatorPrincipalProvider | None = None
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None
    battery_qualification: str = "unsupported"
    battery_operational_status: str = "unconfigured"
    bootstrap_manifest: RuntimeBootstrapManifest | None = None
    commissioning_service: CommissioningService | None = None
    commissioning_report: CommissioningReport | None = None
    dispatchable_battery_binding: DispatchableBatteryBinding | None = None
    ev_actuators: tuple[EVActuator, ...] = ()
    battery_control_coordinator: BatteryControlCoordinator | None = None
    ev_control_coordinators: tuple[EVControlCoordinator, ...] = ()
    control_supervisor: ControlSupervisorPort | None = None
    blocking_workers: list[ClosableWorker] = field(default_factory=list)
    ownership: RuntimeOwnership | None = None
    lifecycle: RuntimeLifecycle = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.lifecycle = RuntimeLifecycle(
            event_runner=self.event_consumer.run,
            scheduler_runner=self.scheduler.run,
            state_refresh_runner=(
                self.state_refresher.run if self.state_refresher is not None else None
            ),
            supervisor_runner=(
                self.run_control_supervisor
                if self.control_supervisor is not None
                else None
            ),
        )

    @property
    def battery_supervisor_task(self) -> asyncio.Task[None] | None:
        """Compatibility view over the lifecycle-owned supervisor task."""

        return self.lifecycle.task("domoai-control-supervisor")

    async def start(self) -> None:
        await self.lifecycle.start()

    async def create_backup(self, output_dir: Path) -> BackupManifest:
        """Create an admin backup through both owned storage lanes.

        This is intentionally an application-level administrative hook. It is
        not registered as an MCP tool and does not grant restore authority to
        an agent.
        """

        return await BackupService(clock=self.clock).create(
            sources=(
                BackupSource("operational", self.database, self.storage),
                BackupSource("audit", self.audit_database, self.audit_storage),
            ),
            output_dir=output_dir,
            deployment_id=self.settings.mcp_deployment_id,
        )

    def register_blocking_worker(self, worker: _W) -> _W:
        """Give this composition ownership of a blocking worker (thread- or
        process-backed) so `close()` shuts it down. Callers that construct
        an `OptimizationWorker`/`ProcessOptimizationWorker` after
        `build_runtime` returns (e.g. the MCP entry points wiring the
        optimizer/energy-context boundaries) MUST route it through here --
        an unregistered worker's threads/processes outlive the runtime.

        Generic over `_W` (not just `ClosableWorker`) so callers keep the
        concrete worker type -- they usually need more than `close()` (e.g.
        `.optimize()`, `.last_wall_time_seconds`) from what this returns."""

        self.blocking_workers.append(worker)
        return worker

    async def run_battery_control_supervisor(self) -> None:
        """Keep physical battery lease ownership supervised while the host runs."""

        coordinator = self.battery_control_coordinator
        if coordinator is None:
            return
        interval = min(max(coordinator.policy.lease_seconds / 4, 0.25), 30.0)
        while True:
            stopped = await coordinator.supervise_once()
            for plan_id in stopped:
                self.audit.append(
                    event_type="control_supervisor_lease_stop",
                    actor="runtime",
                    subject_id=plan_id,
                    payload={"reason": "lease_renewal_unavailable_or_failed"},
                )
            await asyncio.sleep(interval)

    async def run_control_supervisor(self) -> None:
        """Supervise every configured latched actuator in this composition."""

        supervisor = self.control_supervisor
        if supervisor is None:
            return
        policies = [
            getattr(coordinator, "policy", None)
            for coordinator in getattr(supervisor, "coordinators", (supervisor,))
        ]
        lease_seconds = [
            float(policy.lease_seconds)
            for policy in policies
            if policy is not None
        ]
        interval = min(max(min(lease_seconds, default=300.0) / 4, 0.25), 30.0)
        while True:
            stopped = await supervisor.supervise_once()
            for plan_id in stopped:
                self.audit.append(
                    event_type="control_supervisor_lease_stop",
                    actor="runtime",
                    subject_id=plan_id,
                    payload={"reason": "lease_renewal_unavailable_or_failed"},
                )
            await asyncio.sleep(interval)

    async def close(self) -> None:
        await self.lifecycle.close()
        try:
            if self.control_supervisor is not None:
                await self.control_supervisor.shutdown()
            await self.adapter.disconnect()
        finally:
            # Release the singleton only after background workers and physical
            # adapters have stopped, but before any storage cleanup that can
            # fail or be interrupted.  A graceful/interrupt shutdown must
            # never leave a false active owner that blocks the next bootstrap.
            if self.ownership is not None:
                await self.ownership.release()
            for worker in self.blocking_workers:
                await asyncio.to_thread(worker.close)
            for close in self.energy_closers:
                close()
            await self.storage.close()
            await self.audit_storage.close()
            await self.database.close()
            await self.approval_database.close()
            await self.audit_database.close()


def _battery_operational_status(
    registry: DeviceRegistry,
    state_store: StateStore,
    *,
    battery_qualification: str,
    dispatchable_battery_binding: DispatchableBatteryBinding | None,
) -> str:
    """Describe battery integration without turning telemetry into authority."""

    if dispatchable_battery_binding is not None:
        return battery_qualification
    for device in registry.devices:
        if not any(capability.name == "battery.soc" for capability in device.capabilities):
            continue
        snapshot = state_store.peek(device.id, "battery.soc")
        if snapshot is not None and snapshot.value is not None and snapshot.status.value in {
            "current",
            "stale",
        }:
            return "observed-only"
    return "unconfigured"


async def build_runtime(
    settings: Settings | None = None,
    *,
    adapter: AdapterPort | None = None,
    energy_context_provider: EnergyContextProvider | None = None,
    dispatchable_battery_binding: DispatchableBatteryBinding | None = None,
    ev_actuators: tuple[EVActuator, ...] = (),
    ev_charging_bindings: tuple[EVChargingBinding, ...] = (),
    operator_principal_provider: OperatorPrincipalProvider | None = None,
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None,
    clock: Clock | None = None,
    require_configured_adapter: bool = False,
) -> RuntimeComposition:
    resolved_settings = settings or Settings.from_environment()
    bootstrap = RuntimeBootstrap.resolve(resolved_settings)
    resolved_settings = bootstrap.settings
    if energy_context_provider is not None:
        # Spec 161: fail closed before any other startup side effect (no
        # SQLite file, no adapter connection) rather than accepting a
        # supplied provider that either can't be used (energy_live off) or
        # doesn't satisfy the minimum EnergyContextProvider contract.
        if not resolved_settings.energy_live:
            raise ValueError("energy_context_provider requires energy_live to be enabled")
        if not callable(getattr(energy_context_provider, "get_context", None)):
            raise ValueError(
                "energy_context_provider must implement get_context(horizon) -> EnergyContext"
            )
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
    # Spec 162 convergence: mirrors the battery pattern above, but plural
    # (a household can have more than one EV, unlike the single dispatchable
    # battery) -- loaded bindings are concatenated with any explicitly
    # supplied ones so mcp/stdio.py's argument-free build_configured_server()
    # call can reach a configured charger via environment alone.
    ev_charging_bindings = tuple(ev_charging_bindings) + tuple(
        load_ev_charging_binding(path) for path in resolved_settings.ev_charging_binding_paths
    )
    # An EV charging binding is the server-owned authority for both the
    # provider-observed planning state and the corresponding physical command
    # surface.  Keeping the actuator only in the separate optional argument
    # would let a configured gateway plan an EV while silently omitting the
    # JIT write guard and command allowlist.  Derive the actuator view from
    # every binding so settings-driven MCP/stdio deployments cannot lose that
    # boundary between configuration and execution.
    ev_actuators = tuple(ev_actuators) + tuple(
        binding.actuator for binding in ev_charging_bindings
    )
    ev_device_ids = [actuator.device_id for actuator in ev_actuators]
    if len(ev_device_ids) != len(set(ev_device_ids)):
        raise ValueError("EV actuator bindings must target distinct devices")
    if dispatchable_battery_binding is not None and (
        dispatchable_battery_binding.device_id in set(ev_device_ids)
    ):
        raise ValueError("a device cannot be bound as both battery and EV actuator")
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
    # Resolve the physical takeover owner before opening any runtime storage.
    # A provider identity in a binding must resolve to one concrete adapter;
    # falling back to the composite would turn a routing container into an
    # actuator authority and leak resources on a malformed deployment.
    registry = DeviceRegistry()
    provider_registry = ProviderRegistry(clock=clock)
    selected_adapter = adapter or create_adapter(
        resolved_settings,
        registry=registry,
        provider_registry=provider_registry,
        clock=clock,
        dispatchable_battery_binding=dispatchable_battery_binding,
        ev_charging_bindings=ev_charging_bindings,
        require_configured_adapter=require_configured_adapter,
    )
    selected_control_adapter: ControlTakeoverAdapter | None = None
    if dispatchable_battery_binding is not None and dispatchable_battery_binding.profile.actuator:
        selected_control_adapter = _select_control_adapter(
            selected_adapter, dispatchable_battery_binding.provider_id
        )
        bind_dispatchable_battery = getattr(
            selected_control_adapter, "bind_dispatchable_battery", None
        )
        if callable(bind_dispatchable_battery):
            bind_dispatchable_battery(dispatchable_battery_binding)
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
    ev_providers = tuple(
        StateStoreEVProvider.from_binding(state_store=state_store, binding=binding)
        for binding in ev_charging_bindings
    )
    energy_closers: tuple[Callable[[], None], ...]
    if energy_context_provider is not None:
        # Spec 161: an externally-supplied provider bypasses
        # _create_energy_context_provider (and its omie/open_meteo
        # requirement) entirely -- the caller owns composing and, if
        # needed, closing its own sub-providers; nothing here manages its
        # lifecycle beyond holding the reference, same as adapter=. The EV
        # binding's state-provider role is bypassed in this combination, but
        # its actuator role remains active above so a configured binding never
        # loses the execution authorization/guard boundary.
        energy_closers = ()
    else:
        energy_context_provider, energy_closers = _create_energy_context_provider(
            resolved_settings,
            battery_provider=battery_provider,
            ev_providers=ev_providers,
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
    # Audit gets its own admission queue/worker thread and its own SQLite
    # connection. Separate queues prevent admission starvation; separate
    # connections preserve the single-owner transaction invariant of each
    # storage worker instead of relying on check_same_thread=False to make
    # concurrent use of one sqlite3.Connection safe.
    audit_storage = SerializedStorageExecutor(
        queue_capacity=resolved_settings.sqlite_worker_queue_capacity,
        queue_wait_seconds=resolved_settings.sqlite_worker_queue_wait_seconds,
        operation_timeout_seconds=resolved_settings.sqlite_operation_timeout_seconds,
    )
    await storage.run_async(database.initialize)
    audit_path = resolved_settings.audit_database_path or resolved_settings.database_path.with_name(
        f"{resolved_settings.database_path.stem}-audit{resolved_settings.database_path.suffix}"
    )
    audit_database = SQLiteDatabase(
        audit_path,
        busy_timeout_ms=resolved_settings.sqlite_busy_timeout_ms,
        clock=clock,
    )
    await audit_storage.run_async(audit_database.initialize)
    approval_database = SQLiteDatabase(
        resolved_settings.database_path,
        busy_timeout_ms=resolved_settings.sqlite_busy_timeout_ms,
        clock=clock,
    )
    await approval_database.initialize()
    raw_audit_repository = AuditEventRepository(audit_database)
    raw_approval_repository = ApprovalGrantRepository(approval_database)
    approval_store = ApprovalStore(
        operator_token=(
            resolved_settings.operator_approval_token.get_secret_value()
            if resolved_settings.operator_approval_token is not None
            else None
        ),
        allow_legacy_token=resolved_settings.allow_legacy_operator_token,
        clock=clock,
        persistence=raw_approval_repository,
    )
    raw_device_repository = DeviceRepository(database, clock=clock)
    raw_state_snapshot_repository = StateSnapshotRepository(database)
    raw_runtime_state_metadata_repository = RuntimeStateMetadataRepository(database, clock=clock)
    raw_state_persistence_repository = RuntimeStatePersistenceRepository(database, clock=clock)
    audit_repository = cast(
        AuditEventRepository, SerializedRepositoryProxy(raw_audit_repository, audit_storage)
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
    registry.load_persisted(await device_repository.list_all())
    ownership_repository = RuntimeOwnershipRepository(database, clock=clock)
    try:
        ownership = await RuntimeOwnership.acquire(
            ownership_repository,
            resolved_settings,
            adapter_id=selected_adapter.adapter_id,
        )
    except Exception:
        await storage.close()
        await audit_storage.close()
        await database.close()
        await approval_database.close()
        await audit_database.close()
        raise
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
    commissioning_service = CommissioningService(
        registry,
        clock=clock,
        manifest_path=(
            resolved_settings.commissioning_manifest_path
            or resolved_settings.database_path.with_name("commissioning-manifest.json")
        ),
    )
    try:
        commissioning_report = commissioning_service.inspect(
            runtime_revision=state_store.runtime_revision
        )
    except CommissioningPersistenceError as error:
        # A diagnostic report must not prevent a safe runtime from starting or
        # make the authority transaction look incomplete. Keep the report in
        # memory and leave the bounded persistence failure in the audit lane.
        commissioning_report = commissioning_service.inspect(
            runtime_revision=state_store.runtime_revision,
            persist=False,
        )
        audit.append(
            event_type="commissioning_report_persistence_failed",
            actor="runtime",
            subject_id="commissioning",
            payload={"error": str(error)},
        )
    else:
        audit.append(
            event_type="commissioning_report_generated",
            actor="runtime",
            subject_id="commissioning",
            payload={
                "runtime_revision": commissioning_report.runtime_revision,
                "candidates": len(commissioning_report.candidates),
                "report_digest": commissioning_report.report_digest,
            },
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
    authorized_actuator_commands: dict[str, frozenset[str]] = {}
    if dispatchable_battery_binding is not None:
        actuator = dispatchable_battery_binding.profile.actuator
        if actuator is not None:
            authorized_actuator_commands[dispatchable_battery_binding.device_id] = frozenset(
                {actuator.charge_command, actuator.discharge_command, actuator.stop_command}
            )
    for ev_actuator in ev_actuators:
        authorized_actuator_commands[ev_actuator.device_id] = frozenset(
            {ev_actuator.charge_command, ev_actuator.stop_command}
        )
    plan_service = PlanService(
        registry,
        state_store,
        policy_engine,
        audit,
        clock=clock,
        authorized_actuator_commands=authorized_actuator_commands,
    )
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
    scheduled_plan_repository = cast(
        ScheduledPlanRepository,
        SerializedRepositoryProxy(ScheduledPlanRepository(database, clock=clock), storage),
    )
    bundle_commit_repository = cast(
        BundleCommitRepository,
        SerializedRepositoryProxy(BundleCommitRepository(database, clock=clock), storage),
    )
    battery_control_coordinator = None
    if dispatchable_battery_binding is not None:
        actuator = dispatchable_battery_binding.profile.actuator
        if actuator is not None:
            if selected_control_adapter is None:
                raise RuntimeError("battery takeover adapter was not resolved")
            feedback_routes = registry.routes_for(
                dispatchable_battery_binding.device_id,
                actuator.power_feedback_capability,
            )
            battery_control_coordinator = BatteryControlCoordinator(
                selected_control_adapter,
                dispatchable_battery_binding.control_policy,
                device_id=dispatchable_battery_binding.device_id,
                command_names=frozenset(
                    {
                        actuator.charge_command,
                        actuator.discharge_command,
                        actuator.stop_command,
                    }
                ),
                stop_command=actuator.stop_command,
                stop_unit=actuator.power_unit,
                state_store=state_store,
                power_feedback_capability=actuator.power_feedback_capability,
                power_feedback_source_ref=(
                    feedback_routes[0].source_ref
                    if len(feedback_routes) == 1
                    and feedback_routes[0].available
                    else None
                ),
                power_feedback_tolerance_kw=actuator.power_feedback_tolerance_kw,
                clock=clock,
            )
            startup_reconciled = await battery_control_coordinator.reconcile_startup()
            audit.append(
                event_type="control_supervisor_startup_reconciliation",
                actor="runtime",
                subject_id=dispatchable_battery_binding.device_id,
                payload={"confirmed": startup_reconciled},
            )
    ev_control_coordinators: list[EVControlCoordinator] = []
    for binding in ev_charging_bindings:
        ev_actuator = binding.actuator
        ev_adapter = _select_provider_adapter(selected_adapter, binding.provider_id)
        feedback_routes = registry.routes_for(binding.device_id, ev_actuator.capability)
        cached_feedback = state_store.peek(binding.device_id, ev_actuator.capability)
        feedback_source_ref = (
            next(
                route.source_ref
                for route in feedback_routes
                if route.source_ref.adapter_id == cached_feedback.source_ref.adapter_id
                and route.source_ref.external_id == cached_feedback.source_ref.external_id
            )
            if cached_feedback is not None
            and any(
                route.source_ref.adapter_id == cached_feedback.source_ref.adapter_id
                and route.source_ref.external_id == cached_feedback.source_ref.external_id
                for route in feedback_routes
            )
            else (
                feedback_routes[0].source_ref
                if len(feedback_routes) == 1 and feedback_routes[0].available
                else None
            )
        )
        coordinator = EVControlCoordinator(
            ev_adapter,
            binding.control_policy,
            device_id=binding.device_id,
            command_names=frozenset(
                {ev_actuator.charge_command, ev_actuator.stop_command}
            ),
            stop_command=ev_actuator.stop_command,
            stop_unit=ev_actuator.power_unit,
            state_store=state_store,
            power_feedback_capability=ev_actuator.capability,
            power_feedback_source_ref=feedback_source_ref,
            clock=clock,
        )
        startup_reconciled = await coordinator.reconcile_startup()
        audit.append(
            event_type="control_supervisor_startup_reconciliation",
            actor="runtime",
            subject_id=binding.device_id,
            payload={"confirmed": startup_reconciled, "actuator": "ev"},
        )
        ev_control_coordinators.append(coordinator)
    control_coordinators: list[ControlSupervisorPort] = []
    if battery_control_coordinator is not None:
        control_coordinators.append(battery_control_coordinator)
    control_coordinators.extend(ev_control_coordinators)
    if len(control_coordinators) == 1:
        control_supervisor: ControlSupervisorPort | None = control_coordinators[0]
    elif control_coordinators:
        control_supervisor = ControlTakeoverGroup(control_coordinators)
    else:
        control_supervisor = None
    execution_admission = ExecutionAdmission(
        bundle_repository=bundle_commit_repository,
        approval_store=approval_store,
        audit=audit,
    )
    executor = PlanExecutor(
        selected_adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        outcome_repository=outcome_repository,
        state_snapshot_repository=state_snapshot_repository,
        clock=clock,
        safety_kernel=safety_kernel,
        control_takeover=control_supervisor,
        dynamic_safety_guard=(
            DynamicSafetyGuard(
                state_store,
                (
                    dispatchable_battery_binding.profile
                    if dispatchable_battery_binding is not None
                    else None
                ),
                ev_actuators=ev_actuators,
                clock=clock,
            )
            if (
                dispatchable_battery_binding is not None
                and dispatchable_battery_binding.profile.actuator is not None
            )
            or ev_actuators
            else None
        ),
        execution_admission=execution_admission,
    )
    facade = DomoticsFacade(plan_service, executor)
    event_consumer = RuntimeEventConsumer(
        selected_adapter, discovery, state_store, audit, clock=clock
    )
    state_refresher = RuntimeStateRefresher(
        discovery,
        state_store,
        audit,
        interval_seconds=resolved_settings.state_refresh_interval_seconds,
        inventory_refresh_interval_seconds=resolved_settings.inventory_refresh_interval_seconds,
        adapter=selected_adapter,
        clock=clock,
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
        execution_admission=execution_admission,
        clock=clock,
    )
    plans: dict[str, Plan] = {}
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
    battery_operational_status = _battery_operational_status(
        registry,
        state_store,
        battery_qualification=battery_qualification,
        dispatchable_battery_binding=dispatchable_battery_binding,
    )
    runtime = RuntimeComposition(
        settings=resolved_settings,
        adapter=selected_adapter,
        database=database,
        approval_database=approval_database,
        audit_database=audit_database,
        storage=storage,
        audit_storage=audit_storage,
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
        state_refresher=state_refresher,
        energy_context_provider=energy_context_provider,
        battery_provider=battery_provider,
        energy_closers=energy_closers,
        clock=clock,
        operator_principal_provider=operator_principal_provider,
        operator_approval_assertion_provider=operator_approval_assertion_provider,
        battery_qualification=battery_qualification,
        battery_operational_status=battery_operational_status,
        bootstrap_manifest=bootstrap.manifest,
        commissioning_service=commissioning_service,
        commissioning_report=commissioning_report,
        dispatchable_battery_binding=dispatchable_battery_binding,
        ev_actuators=ev_actuators,
        battery_control_coordinator=battery_control_coordinator,
        ev_control_coordinators=tuple(ev_control_coordinators),
        control_supervisor=control_supervisor,
        ownership=ownership,
    )
    return runtime
