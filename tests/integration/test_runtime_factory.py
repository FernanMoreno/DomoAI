import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.application.runtime_factory import build_runtime, create_adapter
from domoai.config.settings import Settings
from domoai.domain.models import Command, Plan, PlanStatus, SourceRef, StateStatus
from domoai.domain.provider import MeasurementQuality
from domoai.optimizer.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatteryProfile,
    BatterySocObservation,
    DispatchableBatteryBinding,
)
from domoai.optimizer.providers import ComposedEnergyContextProvider, StateStoreBatteryProvider
from domoai.persistence.repositories import (
    AuditEventRepository,
    DeviceRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
    StateSnapshotRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.provider_sdk import ProviderRegistry
from tests.fixtures.knx import mapping_payload
from tests.fixtures.modbus import mapping_payload as modbus_mapping_payload
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


def _runtime_dispatchable_battery_binding() -> DispatchableBatteryBinding:
    observed_at = datetime(2026, 8, 22, 12, tzinfo=UTC)
    return DispatchableBatteryBinding(
        provider_id="home_assistant",
        device_id="battery.home",
        profile=BatteryProfile(
            capacity_kwh=8.0,
            initial_soc_kwh=4.0,
            min_soc_kwh=0.0,
            max_soc_kwh=8.0,
            max_charge_kw=2.0,
            max_discharge_kw=2.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            actuator=BatteryActuator(
                device_id="battery.home",
                capability="battery_power",
                charge_command="charge_battery",
                discharge_command="discharge_battery",
                stop_command="stop_battery",
                power_feedback_capability="battery.power",
                power_feedback_tolerance_kw=0.1,
                soc_reconciliation_capability="battery.soc",
            ),
            initial_soc_observation=BatterySocObservation(
                provider_id="home_assistant",
                device_id="battery.home",
                value_kwh=4.0,
                observed_at=observed_at,
                received_at=observed_at,
                quality=MeasurementQuality.GOOD,
                source_ref=SourceRef(
                    adapter_id="home_assistant",
                    external_id="sensor.battery_soc",
                ),
            ),
        ),
        capacity_evidence=BatteryCapacityEvidence(
            provider_id="home_assistant",
            device_id="battery.home",
            capacity_kwh=8.0,
        ),
    )


def test_create_adapter_selects_fixture_or_home_assistant(tmp_path: Path) -> None:
    assert isinstance(create_adapter(Settings()), SimulatedHomeAdapter)
    assert isinstance(
        create_adapter(
            Settings(
                home_assistant_url="http://home-assistant.test",
                home_assistant_token=SecretStr("fixture-token"),
            )
        ),
        HomeAssistantProviderAdapter,
    )

    dispatch_mapping_path = tmp_path / "home-assistant-battery.json"
    dispatch_mapping_path.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "battery_capacity_bindings": {
                    "sensor.battery_capacity": {
                        "device_id": "ha-battery-1",
                        "semantics": "nominal_capacity",
                        "nominal_capacity_attestation": {
                            "evidence_type": "vendor_documentation",
                            "reference": "https://vendor.example/battery",
                            "subject_model": "Battery",
                            "attested_by": "operator",
                            "attested_at": "2026-08-22T12:00:00Z",
                        },
                    }
                },
                "battery_dispatch_bindings": {
                    "home-battery": {
                        "schema_version": "v1",
                        "device_id": "ha-battery-1",
                        "soc_entity_id": "sensor.battery_soc",
                        "power_feedback_entity_id": "sensor.battery_power",
                        "capacity_entity_id": "sensor.battery_capacity",
                        "capacity_metric": "battery.capacity",
                        "charge": {
                            "entity_id": "number.battery_command",
                            "provider_command": "charge",
                        },
                        "discharge": {
                            "entity_id": "number.battery_command",
                            "provider_command": "discharge",
                        },
                        "stop": {
                            "entity_id": "number.battery_command",
                            "provider_command": "stop",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    route_registry = ProviderRegistry()
    route_adapter = create_adapter(
        Settings(
            home_assistant_url="http://home-assistant.test",
            home_assistant_token=SecretStr("fixture-token"),
            home_assistant_mapping_path=dispatch_mapping_path,
        ),
        provider_registry=route_registry,
    )
    assert isinstance(route_adapter, HomeAssistantProviderAdapter)
    route_provider = route_registry.get("home_assistant")
    assert isinstance(route_provider, HomeAssistantProvider)
    assert set(route_provider.battery_dispatch_bindings) == {"home-battery"}
    assert route_provider.battery_dispatch_bindings["home-battery"].charge.provider_command == (
        "charge"
    )

    plaintext_adapter = create_adapter(Settings(zigbee2mqtt_url="mqtt://broker.test:1884"))
    assert isinstance(plaintext_adapter, Zigbee2MqttAdapter)
    assert plaintext_adapter.transport.port == 1884
    assert plaintext_adapter.transport.tls is False

    tls_adapter = create_adapter(Settings(zigbee2mqtt_url="mqtts://broker.test"))
    assert isinstance(tls_adapter, Zigbee2MqttAdapter)
    assert tls_adapter.transport.port == 8883
    assert tls_adapter.transport.tls is True
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
    modbus_mapping_path.write_text(json.dumps(modbus_mapping_payload()), encoding="utf-8")
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
    assert composite._event_queue_max_size == 1000

    custom_size_composite = create_adapter(
        Settings(
            home_assistant_url="http://home-assistant.test",
            home_assistant_token=SecretStr("fixture-token"),
            modbus_host="192.0.2.20",
            modbus_config_path=modbus_mapping_path,
            composite_event_queue_max_size=42,
        )
    )
    assert isinstance(custom_size_composite, CompositeAdapter)
    assert custom_size_composite._event_queue_max_size == 42

    provider_registry = ProviderRegistry()
    provider_composite = create_adapter(
        Settings(
            home_assistant_url="http://home-assistant.test",
            home_assistant_token=SecretStr("fixture-token"),
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


@pytest.mark.asyncio
async def test_build_runtime_recovers_plans_orphaned_by_a_crash(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    orphaned_plan = Plan(
        id="orphaned-crash-plan-1",
        status=PlanStatus.EXECUTING,
        commands=[
            Command(
                id="orphaned-crash-command-1",
                device_id="garden.garden-pump",
                command="turn_on",
                idempotency_key="orphaned-crash-intent-1",
            )
        ],
    )
    await PlanRepository(database).save(orphaned_plan)
    await database.close()

    runtime = await build_runtime(
        Settings(database_path=database_path),
        adapter=SimulatedHomeAdapter(),
    )
    await runtime.close()

    database = SQLiteDatabase(database_path)
    await database.initialize()
    recovered_plan = await PlanRepository(database).get(orphaned_plan.id)
    audit_events = await AuditEventRepository(database).list_all()

    assert recovered_plan is not None
    assert recovered_plan.status is PlanStatus.UNKNOWN
    assert any(
        event.event_type == "plan_execution_recovered" and event.subject_id == orphaned_plan.id
        for event in audit_events
    )


@pytest.mark.asyncio
async def test_build_runtime_threads_configured_sqlite_busy_timeout(tmp_path: Path) -> None:
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "runtime.sqlite3",
            sqlite_busy_timeout_ms=250,
        ),
        adapter=SimulatedHomeAdapter(),
    )

    cursor = runtime.database.connection.execute("PRAGMA busy_timeout")
    timeout = cursor.fetchone()[0]
    cursor.close()
    await runtime.close()

    assert timeout == 250


@pytest.mark.asyncio
async def test_runtime_factory_persists_devices_and_state_after_discovery(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "persist-runtime.sqlite3"
    runtime = await build_runtime(
        Settings(database_path=database_path),
        adapter=SimulatedHomeAdapter(),
    )
    expected_device_ids = {device.id for device in runtime.registry.devices}
    await runtime.close()

    database = SQLiteDatabase(database_path)
    await database.initialize()
    persisted_devices = await DeviceRepository(database).list_all()
    persisted_states = await StateSnapshotRepository(database).list_all()
    await database.close()

    assert {device.id for device in persisted_devices} == expected_device_ids
    assert persisted_states


@pytest.mark.asyncio
async def test_runtime_factory_restart_restores_devices_and_marks_state_stale(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart-runtime.sqlite3"
    first_run = await build_runtime(
        Settings(database_path=database_path),
        adapter=SimulatedHomeAdapter(),
    )
    original_device_ids = {device.id for device in first_run.registry.devices}
    await first_run.close()

    database = SQLiteDatabase(database_path)
    await database.initialize()
    persisted_devices = await DeviceRepository(database).list_all()
    persisted_states = await StateSnapshotRepository(database).list_all()
    await database.close()

    assert {device.id for device in persisted_devices} == original_device_ids
    assert all(state.status is StateStatus.CURRENT for state in persisted_states)

    from domoai.runtime.registry import DeviceRegistry
    from domoai.runtime.state_store import StateStore

    restored_registry = DeviceRegistry()
    restored_registry.load_persisted(persisted_devices)
    restored_store = StateStore()
    restored_store.load_persisted(persisted_states)

    assert {device.id for device in restored_registry.devices} == original_device_ids
    for state in persisted_states:
        restored = await restored_store.get(state.device_id, state.capability)
        assert restored is not None
        assert restored.status is StateStatus.STALE


@pytest.mark.asyncio
async def test_runtime_factory_persisted_state_stays_current_across_rediscoveries(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "current-runtime.sqlite3"
    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(Settings(database_path=database_path), adapter=adapter)
    light_id = next(
        device.id for device in runtime.registry.devices if device.type.value == "light"
    )

    adapter._find_for_device(light_id)["state"]["brightness"] = 42
    await runtime.discovery.refresh()
    await runtime.close()

    database = SQLiteDatabase(database_path)
    await database.initialize()
    persisted_states = await StateSnapshotRepository(database).list_all()
    await database.close()

    brightness = next(
        state
        for state in persisted_states
        if state.device_id == light_id and state.capability == "brightness"
    )
    assert brightness.value == 42


@pytest.mark.asyncio
async def test_scheduled_plan_executes_after_real_runtime_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "restart-scheduled-runtime.sqlite3"
    first_adapter = SimulatedHomeAdapter()
    first_run = await build_runtime(Settings(database_path=database_path), adapter=first_adapter)
    light_id = next(
        device.id for device in first_run.registry.devices if device.type.value == "light"
    )
    # Leave it pending without sleeping; the first runtime never runs its
    # scheduler, so a just-due timestamp still exercises restart persistence.
    execute_at = datetime.now(UTC) - timedelta(seconds=1)
    validated = first_run.plan_service.validate(
        Plan(
            id="restart-scheduled-plan-1",
            execute_at=execute_at,
            expires_at=execute_at + timedelta(minutes=5),
            commands=[
                Command(
                    id="restart-scheduled-command-1",
                    device_id=light_id,
                    command="set_brightness",
                    value=60,
                    unit="%",
                    idempotency_key="restart-scheduled-intent-1",
                )
            ],
        )
    )
    await first_run.scheduler.schedule(validated)
    await first_run.close()

    second_adapter = SimulatedHomeAdapter()
    second_run = await build_runtime(Settings(database_path=database_path), adapter=second_adapter)
    try:
        results = await second_run.scheduler.run_due(now=execute_at + timedelta(seconds=1))

        assert results == [{"plan_id": validated.id, "outcome": "executed"}]
        assert [command.id for command in second_adapter.calls] == [
            "restart-scheduled-command-1"
        ]
        stored = await second_run.scheduled_plan_repository.get(validated.id)
        assert stored is not None
        assert stored[1] == "executed"
    finally:
        await second_run.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["state", "inventory"])
async def test_restart_changed_dependency_stays_stale_without_adapter_call(
    tmp_path: Path, change: str
) -> None:
    database_path = tmp_path / f"restart-changed-{change}.sqlite3"
    first_run = await build_runtime(
        Settings(database_path=database_path), adapter=SimulatedHomeAdapter()
    )
    light_id = next(
        device.id for device in first_run.registry.devices if device.type.value == "light"
    )
    execute_at = datetime.now(UTC) - timedelta(seconds=1)
    validated = first_run.plan_service.validate(
        Plan(
            id=f"restart-changed-plan-{change}",
            execute_at=execute_at,
            expires_at=execute_at + timedelta(minutes=5),
            commands=[
                Command(
                    id=f"restart-changed-command-{change}",
                    device_id=light_id,
                    command="set_brightness",
                    value=60,
                    unit="%",
                    idempotency_key=f"restart-changed-intent-{change}",
                )
            ],
        )
    )
    await first_run.scheduler.schedule(validated)
    await first_run.close()

    second_adapter = SimulatedHomeAdapter()
    if change == "state":
        second_adapter._find("light.living_room_main")["state"]["brightness"] = 42
    else:
        second_adapter._entities[:] = [
            entity
            for entity in second_adapter._entities
            if entity["entity_id"] != "light.living_room_main"
        ]
    second_run = await build_runtime(Settings(database_path=database_path), adapter=second_adapter)
    try:
        results = await second_run.scheduler.run_due(now=execute_at + timedelta(seconds=1))

        assert results == [{"plan_id": validated.id, "outcome": "error"}]
        assert second_adapter.calls == []
        stored = await second_run.scheduled_plan_repository.get(validated.id)
        assert stored is not None
        assert stored[1] == "pending"
    finally:
        await second_run.close()


@pytest.mark.asyncio
async def test_runtime_factory_reconciled_away_device_does_not_reappear_after_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reconciled-runtime.sqlite3"
    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(Settings(database_path=database_path), adapter=adapter)
    light_id = next(
        device.id for device in runtime.registry.devices if device.type.value == "light"
    )

    adapter._entities[:] = [
        item for item in adapter._entities if item["entity_id"] != "light.living_room_main"
    ]
    await runtime.discovery.refresh()
    assert runtime.registry.get(light_id) is None
    await runtime.close()

    database = SQLiteDatabase(database_path)
    await database.initialize()
    persisted_devices = await DeviceRepository(database).list_all()
    persisted_states = await StateSnapshotRepository(database).list_all()
    await database.close()

    assert light_id not in {device.id for device in persisted_devices}
    assert light_id not in {state.device_id for state in persisted_states}


@pytest.mark.asyncio
async def test_runtime_factory_removed_device_state_does_not_reappear_after_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reconciled-restart-runtime.sqlite3"
    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(Settings(database_path=database_path), adapter=adapter)
    light_id = next(
        device.id for device in runtime.registry.devices if device.type.value == "light"
    )
    other_id = next(device.id for device in runtime.registry.devices if device.id != light_id)

    adapter._entities[:] = [
        item for item in adapter._entities if item["entity_id"] != "light.living_room_main"
    ]
    await runtime.discovery.refresh()
    await runtime.close()

    restarted = await build_runtime(Settings(database_path=database_path), adapter=adapter)
    try:
        restarted_state_ids = {state.device_id for state in await restarted.state_store.all()}
        assert light_id not in restarted_state_ids
        assert other_id in restarted_state_ids
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_runtime_factory_loads_configured_policies(tmp_path: Path) -> None:
    policy_path = tmp_path / "policies.toml"
    policy_path.write_text(
        "[[policies]]\n"
        'id = "deny-vacation-mode"\n'
        'action = "deny"\n'
        "priority = 100\n"
        "[policies.target]\n"
        'device_id = "cover.garage_main"\n',
        encoding="utf-8",
    )
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "policy-runtime.sqlite3", policy_config_path=policy_path),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        assert [policy.id for policy in runtime.plan_service.policy_engine.policies] == [
            "deny-vacation-mode"
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_factory_audits_default_policy_when_unconfigured(tmp_path: Path) -> None:
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "default-policy-runtime.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        assert runtime.plan_service.policy_engine.policies == []
        events = await runtime.audit_repository.list_all()
        assert any(event.event_type == "policy_default_applied" for event in events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_factory_wires_configured_safety_limits(tmp_path: Path) -> None:
    safety_limits_path = tmp_path / "safety-limits.toml"
    safety_limits_path.write_text(
        '[[limits]]\ndevice_type = "climate"\ncapability = "target_temperature"\nmaximum = 22\n',
        encoding="utf-8",
    )
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "safety-runtime.sqlite3",
            safety_limits_path=safety_limits_path,
        ),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        device_id = next(
            device.id for device in runtime.registry.devices if device.type.value == "climate"
        )
        plan = runtime.plan_service.create_plan(
            "safety-runtime-plan-1",
            [
                Command(
                    id="safety-runtime-command-1",
                    device_id=device_id,
                    command="set_temperature",
                    value=25,
                    unit="°C",
                    idempotency_key="safety-runtime-intent-1",
                )
            ],
        )
        validated = runtime.facade.validate_plan(plan)
        summary = await runtime.facade.execute_plan(validated)

        assert summary.outcomes[0].status.value == "rejected"
        assert summary.outcomes[0].error is not None
        assert summary.outcomes[0].error.code == "safety_limit_exceeded"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_factory_audits_default_safety_kernel_when_unconfigured(
    tmp_path: Path,
) -> None:
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "default-safety-runtime.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        events = await runtime.audit_repository.list_all()
        assert any(event.event_type == "safety_kernel_default_applied" for event in events)
    finally:
        await runtime.close()


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
        assert isinstance(runtime.energy_context_provider, ComposedEnergyContextProvider)
        assert runtime.energy_context_provider.battery is None
        assert runtime.battery_provider is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_factory_installs_explicit_dispatchable_battery_binding(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_path=tmp_path / "energy-battery.sqlite3",
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

    runtime = await build_runtime(
        settings,
        adapter=SimulatedHomeAdapter(),
        dispatchable_battery_binding=_runtime_dispatchable_battery_binding(),
    )
    try:
        assert isinstance(runtime.battery_provider, StateStoreBatteryProvider)
        assert runtime.battery_provider.device_id == "battery.home"
        assert isinstance(runtime.energy_context_provider, ComposedEnergyContextProvider)
        assert runtime.energy_context_provider.battery is runtime.battery_provider
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_factory_rejects_battery_binding_when_energy_is_disabled(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "disabled-energy-battery.sqlite3"

    with pytest.raises(ValueError, match="energy_live"):
        await build_runtime(
            Settings(database_path=database_path),
            adapter=SimulatedHomeAdapter(),
            dispatchable_battery_binding=_runtime_dispatchable_battery_binding(),
        )

    assert not database_path.exists()


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


@pytest.mark.asyncio
async def test_build_runtime_starts_degraded_when_adapter_connect_fails(
    tmp_path: Path,
) -> None:
    adapter = RecordingAdapter("flaky", source_snapshot(adapter_id="flaky"), fail_connect=True)
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "degraded-connect.sqlite3"), adapter=adapter
    )

    assert runtime.registry.devices == []
    audit_events = await runtime.audit_repository.list_all()
    assert any(event.event_type == "runtime_started_degraded" for event in audit_events)
    await runtime.close()


@pytest.mark.asyncio
async def test_build_runtime_starts_degraded_when_discovery_fails(tmp_path: Path) -> None:
    adapter = RecordingAdapter("flaky", source_snapshot(adapter_id="flaky"), fail_discover=True)
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "degraded-discover.sqlite3"), adapter=adapter
    )

    assert runtime.registry.devices == []
    audit_events = await runtime.audit_repository.list_all()
    assert any(event.event_type == "runtime_started_degraded" for event in audit_events)
    await runtime.close()
