import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.home_assistant.adapter import HomeAssistantAdapter
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.application.runtime_factory import build_runtime, create_adapter
from domoai.config.settings import Settings
from domoai.domain.models import Command
from domoai.optimizer.providers import ComposedEnergyContextProvider
from domoai.persistence.repositories import (
    AuditEventRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.provider_sdk import ProviderRegistry
from tests.fixtures.knx import mapping_payload
from tests.fixtures.modbus import mapping_payload as modbus_mapping_payload


def test_create_adapter_selects_fixture_or_home_assistant(tmp_path: Path) -> None:
    assert isinstance(create_adapter(Settings()), SimulatedHomeAdapter)
    assert isinstance(
        create_adapter(
            Settings(
                home_assistant_url="http://home-assistant.test",
                home_assistant_token=SecretStr("fixture-token"),
            )
        ),
        HomeAssistantAdapter,
    )
    assert isinstance(
        create_adapter(
            Settings(
                home_assistant_url="http://home-assistant.test",
                home_assistant_token=SecretStr("fixture-token"),
                home_assistant_provider=True,
            )
        ),
        HomeAssistantProviderAdapter,
    )
    assert isinstance(
        create_adapter(Settings(zigbee2mqtt_url="mqtt://broker.test:1884")),
        Zigbee2MqttAdapter,
    )
    assert isinstance(
        create_adapter(Settings(matter_server_url="ws://matter.test:5580/ws")),
        MatterServerAdapter,
    )
    mapping_path = tmp_path / "knx.json"
    mapping_path.write_text(json.dumps(mapping_payload()), encoding="utf-8")
    assert isinstance(
        create_adapter(
            Settings(
                knx_gateway_host="192.0.2.10",
                knx_config_path=mapping_path,
            )
        ),
        KnxAdapter,
    )
    modbus_mapping_path = tmp_path / "modbus.json"
    modbus_mapping_path.write_text(
        json.dumps(modbus_mapping_payload()), encoding="utf-8"
    )
    assert isinstance(
        create_adapter(
            Settings(
                modbus_host="192.0.2.20",
                modbus_config_path=modbus_mapping_path,
            )
        ),
        ModbusAdapter,
    )
    composite = create_adapter(
        Settings(
            home_assistant_url="http://home-assistant.test",
            home_assistant_token=SecretStr("fixture-token"),
            modbus_host="192.0.2.20",
            modbus_config_path=modbus_mapping_path,
        )
    )
    assert isinstance(composite, CompositeAdapter)
    assert {adapter.adapter_id for adapter in composite.adapters} == {
        "home_assistant",
        "modbus",
    }
    provider_registry = ProviderRegistry()
    provider_composite = create_adapter(
        Settings(
            home_assistant_url="http://home-assistant.test",
            home_assistant_token=SecretStr("fixture-token"),
            home_assistant_provider=True,
            modbus_host="192.0.2.20",
            modbus_config_path=modbus_mapping_path,
        ),
        provider_registry=provider_registry,
    )
    assert isinstance(provider_composite, CompositeAdapter)
    provider = provider_registry.get("home_assistant")
    assert isinstance(provider, HomeAssistantProvider)
    assert provider.manifest.provider_id == "home_assistant"
    assert {adapter.adapter_id for adapter in provider_composite.adapters} == {
        "home_assistant",
        "modbus",
    }
    with pytest.raises(ValueError, match="DOMOAI_MATTER_SERVER_URL"):
        create_adapter(Settings(matter_server_url="http://matter.test:5580/ws"))

    with pytest.raises(ValueError, match="DOMOAI_MODBUS_HOST"):
        create_adapter(
            Settings(
                modbus_host=" ",
                modbus_config_path=modbus_mapping_path,
            )
        )


@pytest.mark.asyncio
async def test_runtime_factory_wires_sqlite_repositories_and_audit(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    runtime = await build_runtime(
        Settings(database_path=database_path),
        adapter=SimulatedHomeAdapter(),
    )
    device_id = next(
        device.id for device in runtime.registry.devices if device.type.value == "light"
    )
    plan = runtime.plan_service.create_plan(
        "factory-plan-1",
        [
            Command(
                id="factory-command-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                idempotency_key="factory-intent-1",
            )
        ],
    )

    validated = runtime.facade.validate_plan(plan)
    summary = await runtime.facade.execute_plan(validated)
    await runtime.close()

    database = SQLiteDatabase(database_path)
    await database.initialize()
    recovered_plan = await PlanRepository(database).get(plan.id)
    recovered_outcomes = await ExecutionOutcomeRepository(database).list_for_plan(plan.id)
    audit_events = await AuditEventRepository(database).list_all()

    assert recovered_plan is not None
    assert recovered_plan.execution is not None
    assert recovered_plan.execution.outcomes == summary.outcomes
    assert recovered_outcomes == summary.outcomes
    assert any(event.event_type == "plan_validated" for event in audit_events)
    await database.close()


@pytest.mark.asyncio
async def test_runtime_factory_builds_live_energy_provider_only_when_enabled(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_path=tmp_path / "energy.sqlite3",
        energy_live=True,
        tariff_provider="omie",
        solar_provider="open_meteo",
        solar_latitude=40.4168,
        solar_longitude=-3.7038,
        solar_installed_kwp=6,
        solar_tilt=30,
        solar_azimuth=0,
        solar_performance_ratio=0.82,
    )

    runtime = await build_runtime(settings, adapter=SimulatedHomeAdapter())
    try:
        assert runtime.energy_context_provider is not None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_factory_builds_live_energy_provider_from_persisted_profile(
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "solar-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "profile_id": "home",
                "latitude": 40.4168,
                "longitude": -3.7038,
                "installed_kwp": 6.0,
                "tilt": 30.0,
                "azimuth": 0.0,
                "performance_ratio": 0.82,
                "inverter_ac_max_kw": 5.0,
                "timezone": "Europe/Madrid",
                "source_id": "operator_config",
                "source_revision": "test",
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        database_path=tmp_path / "energy-profile.sqlite3",
        energy_live=True,
        tariff_provider="omie",
        solar_provider="open_meteo",
        solar_profile_path=profile_path,
    )

    runtime = await build_runtime(settings, adapter=SimulatedHomeAdapter())
    try:
        assert runtime.energy_context_provider is not None
        assert isinstance(runtime.energy_context_provider, ComposedEnergyContextProvider)
        solar_provider = runtime.energy_context_provider.solar
        assert solar_provider.config.latitude == 40.4168
        assert solar_provider.config.installed_kwp == 6
    finally:
        await runtime.close()
