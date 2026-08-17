"""Configurable composition root for fixture and live runtime deployments."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.home_assistant.adapter import HomeAssistantAdapter
from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.adapters.home_assistant.config import load_metric_mappings
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
from domoai.config.settings import Settings
from domoai.config.solar_profile import resolve_solar_profile
from domoai.optimizer.omie import OmieTariffHttpClient, OmieTariffProvider
from domoai.optimizer.open_meteo import (
    OpenMeteoHttpClient,
    OpenMeteoSolarConfig,
    OpenMeteoSolarProvider,
)
from domoai.optimizer.ports import EnergyContextProvider
from domoai.optimizer.providers import ComposedEnergyContextProvider
from domoai.persistence.repositories import (
    AuditEventRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.event_consumer import RuntimeEventConsumer
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.ports import AdapterPort
from domoai.runtime.provider_sdk import ProviderRegistry
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def create_adapter(
    settings: Settings,
    *,
    registry: DeviceRegistry | None = None,
    provider_registry: ProviderRegistry | None = None,
) -> AdapterPort:
    adapters = _create_configured_adapters(settings, provider_registry=provider_registry)
    if not adapters:
        return SimulatedHomeAdapter()
    if len(adapters) == 1:
        return adapters[0]
    return CompositeAdapter(adapters, registry=registry)


def _create_configured_adapters(
    settings: Settings, *, provider_registry: ProviderRegistry | None = None
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
        metric_mappings = (
            load_metric_mappings(settings.home_assistant_mapping_path)
            if settings.home_assistant_mapping_path is not None
            else None
        )
        if settings.home_assistant_provider:
            client = HomeAssistantClient(
                settings.home_assistant_url,
                settings.home_assistant_token.get_secret_value(),
            )
            provider = HomeAssistantProvider(
                client,
                metric_mappings=metric_mappings,
            )
            if provider_registry is not None:
                provider_registry.register(provider)
            adapters.append(HomeAssistantProviderAdapter(provider))
        else:
            adapters.append(
                HomeAssistantAdapter(
                    HomeAssistantClient(
                        settings.home_assistant_url,
                        settings.home_assistant_token.get_secret_value(),
                    )
                )
            )
    if settings.zigbee2mqtt_url is not None:
        parsed = urlparse(settings.zigbee2mqtt_url)
        if parsed.scheme not in {"mqtt", "mqtts"} or parsed.hostname is None:
            raise ValueError("DOMOAI_ZIGBEE2MQTT_URL must be a valid mqtt:// or mqtts:// URL")
        if parsed.scheme == "mqtts":
            raise ValueError("mqtts:// is not supported by the initial Zigbee2MQTT transport")
        adapters.append(
            Zigbee2MqttAdapter(
                AiomqttTransport(
                    parsed.hostname,
                    port=parsed.port or 1883,
                    username=settings.mqtt_username,
                    password=(
                        settings.mqtt_password.get_secret_value()
                        if settings.mqtt_password is not None
                        else None
                    ),
                    timeout=settings.mqtt_timeout_seconds,
                ),
                base_topic=settings.zigbee2mqtt_base_topic,
                discovery_timeout=settings.mqtt_timeout_seconds,
            )
        )
    if settings.matter_server_url is not None:
        parsed = urlparse(settings.matter_server_url)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname is None:
            raise ValueError(
                "DOMOAI_MATTER_SERVER_URL must be a valid ws:// or wss:// URL"
            )
        adapters.append(
            MatterServerAdapter(
                MatterServerWebSocketTransport(
                    settings.matter_server_url,
                    timeout=settings.matter_timeout_seconds,
                ),
                discovery_timeout=settings.matter_timeout_seconds,
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
                ),
                mapping,
                discovery_timeout=settings.knx_timeout_seconds,
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
                ),
                modbus_mapping,
                discovery_timeout=settings.modbus_timeout_seconds,
                poll_interval=settings.modbus_poll_interval_seconds,
            ),
        )
    return adapters


def _create_energy_context_provider(
    settings: Settings,
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
    omie_client = OmieTariffHttpClient(timeout=settings.omie_timeout_seconds)
    solar_client = OpenMeteoHttpClient(timeout=settings.solar_timeout_seconds)
    provider = ComposedEnergyContextProvider(
        tariffs=OmieTariffProvider(omie_client),
        solar=OpenMeteoSolarProvider(solar_client, solar_config),
        max_age_seconds=settings.energy_max_age_seconds,
    )
    return provider, (omie_client.close, solar_client.close)


@dataclass
class RuntimeComposition:
    settings: Settings
    adapter: AdapterPort
    database: SQLiteDatabase
    audit_repository: AuditEventRepository
    plan_repository: PlanRepository
    outcome_repository: ExecutionOutcomeRepository
    registry: DeviceRegistry
    provider_registry: ProviderRegistry
    state_store: StateStore
    audit: AuditLog
    discovery: DiscoveryService
    plan_service: PlanService
    facade: DomoticsFacade
    event_consumer: RuntimeEventConsumer
    energy_context_provider: EnergyContextProvider | None = None
    energy_closers: tuple[Callable[[], None], ...] = ()

    async def close(self) -> None:
        try:
            await self.adapter.disconnect()
        finally:
            for close in self.energy_closers:
                close()
            await self.database.close()


async def build_runtime(
    settings: Settings | None = None, *, adapter: AdapterPort | None = None
) -> RuntimeComposition:
    resolved_settings = settings or Settings.from_environment()
    energy_context_provider, energy_closers = _create_energy_context_provider(resolved_settings)
    database = SQLiteDatabase(resolved_settings.database_path)
    await database.initialize()
    audit_repository = AuditEventRepository(database)
    audit = AuditLog(sink=audit_repository)
    registry = DeviceRegistry()
    provider_registry = ProviderRegistry()
    selected_adapter = adapter or create_adapter(
        resolved_settings,
        registry=registry,
        provider_registry=provider_registry,
    )
    if isinstance(selected_adapter, CompositeAdapter):
        selected_adapter.bind_registry(registry)
    state_store = StateStore(
        stale_after=timedelta(seconds=resolved_settings.state_stale_after_seconds)
    )
    await selected_adapter.connect()
    discovery = DiscoveryService(selected_adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    plan_repository = PlanRepository(database)
    outcome_repository = ExecutionOutcomeRepository(database)
    executor = PlanExecutor(
        selected_adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        outcome_repository=outcome_repository,
    )
    facade = DomoticsFacade(plan_service, executor)
    event_consumer = RuntimeEventConsumer(selected_adapter, discovery, state_store, audit)
    return RuntimeComposition(
        settings=resolved_settings,
        adapter=selected_adapter,
        database=database,
        audit_repository=audit_repository,
        plan_repository=plan_repository,
        outcome_repository=outcome_repository,
        registry=registry,
        provider_registry=provider_registry,
        state_store=state_store,
        audit=audit,
        discovery=discovery,
        plan_service=plan_service,
        facade=facade,
        event_consumer=event_consumer,
        energy_context_provider=energy_context_provider,
        energy_closers=energy_closers,
    )
